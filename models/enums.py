from enum import Enum

class SentimentType(str, Enum):    
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"

class TopicType(str, Enum):
    PRODUCT_QUALITY = "product_quality"
    CUSTOMER_SERVICE = "customer_service"
    PRICING = "pricing"
    DELIVERY = "delivery"
    USER_EXPERIENCE = "user_experience"
    FEATURES = "features"
    BUGS = "bugs"
    SUGGESTIONS = "suggestions"
    OTHER = "other"

class ActionItemPriorityType(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ProcessingStatus(str, Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    VALIDATION_ERROR = "validation_error"
    SKIPPED_EMPTY = "skipped_empty"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    SKIPPED_INVALID = "skipped_invalid"
