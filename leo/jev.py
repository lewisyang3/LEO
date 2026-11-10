"""The Jev (TypeSafe AI) client, shared by the classifier (B2) and the validator (B4).

The key comes from our `JEV_API_KEY` setting and is passed explicitly, because the SDK's own
environment variable is `TYPESAFE_API_KEY`.
"""

from typesafe_sdk import TypeSafeClient, TypeSafeError

from leo.config import Settings, get_settings

JEV_MODEL = "jev-latest"


class JevUnavailable(RuntimeError):
    """No usable Jev client: the key is missing or the SDK rejected the configuration."""


def jev_client(settings: Settings | None = None) -> TypeSafeClient:
    settings = settings or get_settings()
    key = (settings.jev_api_key or "").strip()
    if not key:
        raise JevUnavailable(
            "JEV_API_KEY is not set. Set it in .env, or run without Jev"
            " (--classifier fixture, --validator rules)."
        )
    try:
        return TypeSafeClient(api_key=key, model=JEV_MODEL)
    except TypeSafeError as e:
        raise JevUnavailable(f"could not create the Jev client: {e}") from e
