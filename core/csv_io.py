"""CSV ingestion, traceable row preparation, and safe result exports."""

import csv
import io
from collections.abc import Sequence
from pathlib import PurePath

import pandas as pd

from core.config import Config
from models.enums import ProcessingStatus
from models.models import FeedbackResult

# The file is already bounded to 5 MB. Do not let the stdlib's smaller default
# field limit reject long metadata columns or feedback that should be skipped.
csv.field_size_limit(max(csv.field_size_limit(), Config.MAX_CSV_BYTES))


class CsvInputError(ValueError):
    """An uploaded CSV or selected feedback column cannot be processed."""


def _row_limit(max_rows: int | None = None) -> int:
    """Explicit larger limits belong to the bounded background-task path."""
    limit = Config.MAX_BATCH_ROWS if max_rows is None else max_rows
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise CsvInputError("记录上限必须为 1 至 1000 的整数。")
    return limit


def _validate_quote_syntax(text: str) -> None:
    """Reject stray quotes that csv.reader otherwise accepts in unquoted fields."""
    in_quotes = False
    field_start = True
    quote_closed = False
    position = 0
    while position < len(text):
        character = text[position]
        if in_quotes:
            if character == '"':
                if position + 1 < len(text) and text[position + 1] == '"':
                    position += 2
                    continue
                in_quotes = False
                quote_closed = True
        elif character == '"':
            if not field_start:
                raise CsvInputError("CSV 存在畸形引号：含双引号的字段需要整体引用，并用两个双引号转义。")
            in_quotes = True
            field_start = False
        elif character in {",", "\r", "\n"}:
            field_start = True
            quote_closed = False
        elif quote_closed:
            raise CsvInputError("CSV 存在畸形引号：闭合双引号后只能跟逗号或换行。")
        else:
            field_start = False
        position += 1
    if in_quotes:
        raise CsvInputError("CSV 存在未闭合的双引号，请检查含逗号或换行的文本。")


def _validate_columns(columns: Sequence[object]) -> None:
    if not columns:
        raise CsvInputError("CSV 必须包含表头和至少一列数据。")
    normalized: list[str] = []
    for column in columns:
        if not isinstance(column, str) or not column.strip():
            raise CsvInputError("CSV 表头不能包含空白列名，请为每一列填写名称。")
        normalized.append(column.strip())
    if len(set(normalized)) != len(normalized):
        raise CsvInputError("CSV 表头包含重复列名（忽略首尾空白），请修改为唯一的列名。")


def read_feedback_csv(
    data: bytes,
    *,
    filename: str | None = None,
    encoding: str = "utf-8-sig",
    max_rows: int | None = None,
) -> pd.DataFrame:
    """Read a strict comma-separated CSV without discarding blank records.

    Row identities refer to logical CSV records: the header is record 1 and
    data begins at record 2. Newlines inside quoted fields do not add records.
    """
    limit = _row_limit(max_rows)
    if not isinstance(data, bytes):
        raise CsvInputError("请提供 CSV 文件的二进制内容。")
    if not data:
        raise CsvInputError("上传的 CSV 文件为空，请选择包含反馈数据的文件。")
    if len(data) > Config.MAX_CSV_BYTES:
        raise CsvInputError(
            f"CSV 文件超过 {Config.MAX_CSV_BYTES // (1024 * 1024)} MB 上限，请拆分文件。"
        )
    if filename is not None and (
        not isinstance(filename, str) or PurePath(filename).suffix.lower() != ".csv"
    ):
        raise CsvInputError("文件格式错误：请上传扩展名为 .csv 的文件。")
    if encoding not in {"utf-8-sig", "gb18030"}:
        raise CsvInputError("不支持该编码，请选择 UTF-8 或 GB18030。")
    try:
        decoded = data.decode(encoding)
    except UnicodeDecodeError as exc:
        raise CsvInputError("CSV 编码无法解码，请切换 UTF-8 / GB18030 或重新保存文件。") from exc
    if not decoded.strip():
        raise CsvInputError("上传的 CSV 文件只有空白内容，请添加表头和反馈数据。")
    if "\x00" in decoded:
        raise CsvInputError("CSV 含有无效的空字符，请重新导出为 UTF-8 或 GB18030 CSV。")
    _validate_quote_syntax(decoded)

    try:
        reader = csv.reader(io.StringIO(decoded, newline=""), strict=True)
        header = next(reader)
        _validate_columns(header)
        row_count = 0
        for source_row, record in enumerate(reader, start=2):
            row_count += 1
            if row_count > limit:
                raise CsvInputError(
                    f"CSV 超过 {limit} 条记录上限（含空白和重复记录），请拆分文件。"
                )
            # An empty physical record is preserved by Pandas as an empty row.
            if record and len(record) != len(header):
                raise CsvInputError(
                    f"CSV 第 {source_row} 条逻辑记录字段数错误："
                    f"需要 {len(header)} 个字段，实际为 {len(record)} 个。"
                    "含逗号或换行的文本必须用双引号包围。"
                )
        if row_count == 0:
            raise CsvInputError("CSV 只有表头，没有可处理的数据记录。")
    except csv.Error as exc:
        raise CsvInputError("CSV 解析失败：存在畸形引号、未闭合文本或过长字段，请检查文件。") from exc
    except StopIteration as exc:
        raise CsvInputError("CSV 缺少有效表头。") from exc

    try:
        frame = pd.read_csv(
            io.StringIO(decoded, newline=""),
            dtype=str,
            keep_default_na=False,
            skip_blank_lines=False,
        )
    except (pd.errors.ParserError, pd.errors.EmptyDataError, ValueError) as exc:
        raise CsvInputError("CSV 无法读取，请检查表头、引号和每条记录的字段数。") from exc
    if len(frame) != row_count:
        raise CsvInputError("CSV 解析记录数不一致，请检查空白记录和换行格式；文件未被处理。")
    _validate_columns(list(frame.columns))
    return frame


def prepare_feedback_rows(frame: pd.DataFrame, text_column: str, *, max_rows: int | None = None) -> list[FeedbackResult]:
    """Keep every source row, marking invalid and duplicate feedback as skipped."""
    limit = _row_limit(max_rows)
    if not isinstance(frame, pd.DataFrame):
        raise CsvInputError("反馈数据必须为 Pandas 数据表。")
    _validate_columns(list(frame.columns))
    if not isinstance(text_column, str) or text_column not in frame.columns:
        raise CsvInputError("请选择 CSV 中存在的反馈文本列。")
    if frame.empty:
        raise CsvInputError("CSV 没有数据记录，请上传包含反馈文本的文件。")
    if len(frame) > limit:
        raise CsvInputError(f"CSV 超过 {limit} 条记录上限，请拆分文件。")

    results: list[FeedbackResult] = []
    first_feedback: dict[str, str] = {}
    for source_row, value in enumerate(frame[text_column].tolist(), start=2):
        feedback_id = f"row-{source_row}"
        missing = value is None
        if not missing and not isinstance(value, str) and pd.api.types.is_scalar(value):
            # Values in manually supplied DataFrames may be non-scalar objects.
            try:
                missing = bool(pd.isna(value))
            except (TypeError, ValueError):
                missing = False
        original_text = "" if missing else (value if isinstance(value, str) else str(value))
        result = FeedbackResult(
            feedback_id=feedback_id,
            source_row=source_row,
            original_text=original_text,
        )
        if missing or (isinstance(value, str) and not value.strip()):
            result.processing_status = ProcessingStatus.SKIPPED_EMPTY
            result.error_message = "反馈为空或仅包含空白，已跳过。"
        elif not isinstance(value, str):
            result.processing_status = ProcessingStatus.SKIPPED_INVALID
            result.error_message = "反馈必须为文本，非文本值已跳过。"
        elif len(value) > Config.MAX_TEXT_LENGTH:
            result.processing_status = ProcessingStatus.SKIPPED_INVALID
            result.error_message = f"反馈超过 {Config.MAX_TEXT_LENGTH} 字符上限，已跳过。"
        else:
            normalized = value.strip()
            if normalized in first_feedback:
                result.processing_status = ProcessingStatus.SKIPPED_DUPLICATE
                result.duplicate_of = first_feedback[normalized]
                result.error_message = "反馈与前面的记录重复，已跳过且不重复调用 API。"
            else:
                first_feedback[normalized] = feedback_id
        results.append(result)
    return results


def results_to_dataframe(results: Sequence[FeedbackResult]) -> pd.DataFrame:
    """Produce stable columns and plain enum values, even for an empty batch."""
    return pd.DataFrame(
        [result.model_dump(mode="json") for result in results],
        columns=list(FeedbackResult.model_fields),
    )


def _escape_csv_cell(value: object) -> object:
    if isinstance(value, str):
        stripped = value.lstrip()
        leading_whitespace = value[: len(value) - len(stripped)]
        if stripped.startswith(("=", "+", "-", "@")) or any(
            character in leading_whitespace for character in ("\t", "\r", "\n")
        ):
            return "'" + value
    return value


def export_results_csv(frame: pd.DataFrame) -> bytes:
    """Export UTF-8 with BOM, escaping formulas without changing the source table."""
    if not isinstance(frame, pd.DataFrame):
        raise CsvInputError("导出结果必须为 Pandas 数据表。")
    escaped = frame.copy(deep=True)
    for column in escaped.columns:
        escaped[column] = escaped[column].map(_escape_csv_cell)
    return escaped.to_csv(index=False).encode("utf-8-sig")
