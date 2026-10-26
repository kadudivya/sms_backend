"""JSON for API responses: ISO dates and times, Decimal as a number, enums as their value."""
import enum
from datetime import date, datetime, time, timezone
from decimal import Decimal

from flask.json.provider import DefaultJSONProvider


class ApiJSONProvider(DefaultJSONProvider):
    sort_keys = True

    @staticmethod
    def default(o):
        if isinstance(o, datetime):
            return (o if o.tzinfo else o.replace(tzinfo=timezone.utc)).isoformat()
        if isinstance(o, (date, time)):
            return o.isoformat()
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, enum.Enum):
            return o.value
        return DefaultJSONProvider.default(o)
