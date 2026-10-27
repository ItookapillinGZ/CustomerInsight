"""Initial accounts, workspace history and durable job schema."""

from alembic import op
import sqlalchemy as sa

revision = "20261022_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('app_login_throttles',
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('window_start', sa.Float(), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('key', name=op.f('pk_app_login_throttles'))
    )
    op.create_index('ix_app_login_throttles_window_start', 'app_login_throttles', ['window_start'], unique=False)
    op.create_table('app_workspaces',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_app_workspaces'))
    )
    op.create_table('analysis_jobs',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('scope', sa.String(length=32), nullable=False),
    sa.Column('generation', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), sa.Identity(always=False), nullable=False),
    sa.Column('dedupe_key', sa.String(length=64), nullable=False),
    sa.Column('source_key', sa.String(length=64), nullable=True),
    sa.Column('report_superseded_by', sa.String(length=32), nullable=True),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('filename', sa.String(length=255), nullable=False),
    sa.Column('original', sa.LargeBinary(), nullable=False),
    sa.Column('original_hash', sa.String(length=64), nullable=False),
    sa.Column('request_json', sa.Text(), nullable=False),
    sa.Column('config_json', sa.Text(), nullable=False),
    sa.Column('max_requests', sa.Integer(), nullable=False),
    sa.Column('actual_requests', sa.Integer(), server_default='0', nullable=False),
    sa.Column('total_rows', sa.Integer(), nullable=False),
    sa.Column('total_groups', sa.Integer(), nullable=False),
    sa.Column('cancel_requested', sa.Integer(), server_default='0', nullable=False),
    sa.Column('batch_id', sa.String(length=32), nullable=True),
    sa.Column('error_code', sa.String(length=80), nullable=True),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.Column('updated_at', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['scope', 'report_superseded_by'], ['analysis_jobs.scope', 'analysis_jobs.id'], name='fk_jobs_superseded_scope'),
    sa.ForeignKeyConstraint(['scope'], ['app_workspaces.id'], name=op.f('fk_analysis_jobs_scope_app_workspaces')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_analysis_jobs')),
    sa.UniqueConstraint('generation', name=op.f('uq_analysis_jobs_generation')),
    sa.UniqueConstraint('scope', 'id', name='uq_jobs_scope_id')
    )
    op.create_index('ix_pg_jobs_dedupe', 'analysis_jobs', ['scope', 'dedupe_key', 'generation'], unique=False)
    op.create_index('ix_pg_jobs_recent', 'analysis_jobs', ['scope', 'created_at', 'id'], unique=False)
    op.create_index('ix_pg_jobs_source', 'analysis_jobs', ['scope', 'source_key'], unique=False)
    op.create_table('app_users',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('username', sa.String(length=64), nullable=False),
    sa.Column('password_hash', sa.Text(), nullable=False),
    sa.Column('workspace_id', sa.String(length=32), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id'], ['app_workspaces.id'], name=op.f('fk_app_users_workspace_id_app_workspaces')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_app_users')),
    sa.UniqueConstraint('username', name=op.f('uq_app_users_username')),
    sa.UniqueConstraint('workspace_id', name=op.f('uq_app_users_workspace_id'))
    )
    op.create_table('history_batches',
    sa.Column('workspace_id', sa.String(length=32), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('mapping_hash', sa.String(length=64), nullable=False),
    sa.Column('filename', sa.Text(), nullable=False),
    sa.Column('original_name', sa.String(length=40), nullable=False),
    sa.Column('created_at', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.Text(), nullable=False),
    sa.Column('report_json', sa.Text(), nullable=False),
    sa.Column('report_hash', sa.String(length=64), nullable=False),
    sa.Column('report_fingerprint', sa.String(length=64), nullable=False),
    sa.Column('mapping_json', sa.Text(), nullable=False),
    sa.Column('chat_json', sa.Text(), nullable=False),
    sa.Column('chat_hash', sa.String(length=64), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id'], ['app_workspaces.id'], name=op.f('fk_history_batches_workspace_id_app_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('workspace_id', 'id', name=op.f('pk_history_batches')),
    sa.UniqueConstraint('workspace_id', 'content_hash', 'mapping_hash', name='uq_history_content_mapping')
    )
    op.create_index('ix_history_workspace_created', 'history_batches', ['workspace_id', 'created_at', 'id'], unique=False)
    op.create_table('history_column_mappings',
    sa.Column('workspace_id', sa.String(length=32), nullable=False),
    sa.Column('signature', sa.String(length=64), nullable=False),
    sa.Column('columns_json', sa.Text(), nullable=False),
    sa.Column('mapping_json', sa.Text(), nullable=False),
    sa.Column('mapping_hash', sa.String(length=64), nullable=False),
    sa.Column('updated_at', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id'], ['app_workspaces.id'], name=op.f('fk_history_column_mappings_workspace_id_app_workspaces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('workspace_id', 'signature', name=op.f('pk_history_column_mappings'))
    )
    op.create_table('analysis_groups',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('job_id', sa.String(length=32), nullable=False),
    sa.Column('scope', sa.String(length=32), nullable=False),
    sa.Column('group_index', sa.Integer(), nullable=False),
    sa.Column('input_json', sa.Text(), nullable=False),
    sa.Column('input_hash', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('lease_token', sa.String(length=32), nullable=True),
    sa.Column('lease_until', sa.Float(), nullable=True),
    sa.Column('worker_id', sa.String(length=255), nullable=True),
    sa.Column('attempt_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('usage_json', sa.Text(), server_default='{}', nullable=False),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.Column('updated_at', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['scope', 'job_id'], ['analysis_jobs.scope', 'analysis_jobs.id'], name='fk_groups_job_scope'),
    sa.ForeignKeyConstraint(['scope'], ['app_workspaces.id'], name=op.f('fk_analysis_groups_scope_app_workspaces')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_analysis_groups')),
    sa.UniqueConstraint('job_id', 'group_index', name='uq_groups_job_index'),
    sa.UniqueConstraint('scope', 'job_id', 'id', name='uq_groups_scope_job_id')
    )
    op.create_index('ix_pg_groups_ready', 'analysis_groups', ['job_id', 'status', 'group_index'], unique=False)
    op.create_table('app_sessions',
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.Column('expires_at', sa.Float(), nullable=False),
    sa.Column('revoked_at', sa.Float(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['app_users.id'], name=op.f('fk_app_sessions_user_id_app_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('token_hash', name=op.f('pk_app_sessions'))
    )
    op.create_index('ix_app_sessions_expires_at', 'app_sessions', ['expires_at'], unique=False)
    op.create_index('ix_app_sessions_user_id', 'app_sessions', ['user_id'], unique=False)
    op.create_table('job_idempotency',
    sa.Column('scope', sa.String(length=32), nullable=False),
    sa.Column('key_hash', sa.String(length=64), nullable=False),
    sa.Column('dedupe_key', sa.String(length=64), nullable=False),
    sa.Column('job_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['scope', 'job_id'], ['analysis_jobs.scope', 'analysis_jobs.id'], name='fk_job_idempotency_scope'),
    sa.ForeignKeyConstraint(['scope'], ['app_workspaces.id'], name=op.f('fk_job_idempotency_scope_app_workspaces')),
    sa.PrimaryKeyConstraint('scope', 'key_hash', name=op.f('pk_job_idempotency'))
    )
    op.create_table('job_outbox',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('job_id', sa.String(length=32), nullable=False),
    sa.Column('scope', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.Column('published_at', sa.Float(), nullable=True),
    sa.ForeignKeyConstraint(['scope', 'job_id'], ['analysis_jobs.scope', 'analysis_jobs.id'], name='fk_outbox_job_scope'),
    sa.ForeignKeyConstraint(['scope'], ['app_workspaces.id'], name=op.f('fk_job_outbox_scope_app_workspaces')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_job_outbox'))
    )
    op.create_index('ix_pg_outbox_pending', 'job_outbox', ['status', 'created_at'], unique=False)
    op.create_table('analysis_attempts',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('job_id', sa.String(length=32), nullable=False),
    sa.Column('group_id', sa.String(length=32), nullable=False),
    sa.Column('scope', sa.String(length=32), nullable=False),
    sa.Column('lease_token', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('usage_json', sa.Text(), server_default='{}', nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.Column('completed_at', sa.Float(), nullable=True),
    sa.ForeignKeyConstraint(['scope', 'job_id', 'group_id'], ['analysis_groups.scope', 'analysis_groups.job_id', 'analysis_groups.id'], name='fk_attempts_group_scope'),
    sa.ForeignKeyConstraint(['scope', 'job_id'], ['analysis_jobs.scope', 'analysis_jobs.id'], name='fk_attempts_job_scope'),
    sa.ForeignKeyConstraint(['scope'], ['app_workspaces.id'], name=op.f('fk_analysis_attempts_scope_app_workspaces')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_analysis_attempts')),
    sa.UniqueConstraint('lease_token', name=op.f('uq_analysis_attempts_lease_token'))
    )
    op.create_table('analysis_results',
    sa.Column('job_id', sa.String(length=32), nullable=False),
    sa.Column('feedback_id', sa.String(length=255), nullable=False),
    sa.Column('source_row', sa.Integer(), nullable=False),
    sa.Column('group_id', sa.String(length=32), nullable=True),
    sa.Column('result_json', sa.Text(), nullable=False),
    sa.Column('scope', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['scope', 'job_id', 'group_id'], ['analysis_groups.scope', 'analysis_groups.job_id', 'analysis_groups.id'], name='fk_results_group_scope'),
    sa.ForeignKeyConstraint(['scope', 'job_id'], ['analysis_jobs.scope', 'analysis_jobs.id'], name='fk_results_job_scope'),
    sa.ForeignKeyConstraint(['scope'], ['app_workspaces.id'], name=op.f('fk_analysis_results_scope_app_workspaces')),
    sa.PrimaryKeyConstraint('job_id', 'feedback_id', name=op.f('pk_analysis_results')),
    sa.UniqueConstraint('job_id', 'source_row', name='uq_results_source_row')
    )

def downgrade():
    op.drop_table('analysis_results')
    op.drop_table('analysis_attempts')
    op.drop_index('ix_pg_outbox_pending', table_name='job_outbox')
    op.drop_table('job_outbox')
    op.drop_table('job_idempotency')
    op.drop_index('ix_app_sessions_user_id', table_name='app_sessions')
    op.drop_index('ix_app_sessions_expires_at', table_name='app_sessions')
    op.drop_table('app_sessions')
    op.drop_index('ix_pg_groups_ready', table_name='analysis_groups')
    op.drop_table('analysis_groups')
    op.drop_table('history_column_mappings')
    op.drop_index('ix_history_workspace_created', table_name='history_batches')
    op.drop_table('history_batches')
    op.drop_table('app_users')
    op.drop_index('ix_pg_jobs_source', table_name='analysis_jobs')
    op.drop_index('ix_pg_jobs_recent', table_name='analysis_jobs')
    op.drop_index('ix_pg_jobs_dedupe', table_name='analysis_jobs')
    op.drop_table('analysis_jobs')
    op.drop_table('app_workspaces')
    op.drop_index('ix_app_login_throttles_window_start', table_name='app_login_throttles')
    op.drop_table('app_login_throttles')
