"""Settings and paths.

Everything tunable lives either here or in data/*.yaml. Nothing that shapes a
decision is buried in a function body.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent.parent
DATA_DIR = PROJECT_ROOT / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- OpenAI ----
    openai_api_key: str | None = None

    # Primary model. Fallbacks are tried in order on persistent failure.
    model_primary: str = "gpt-5.6-luna"
    model_fallbacks: list[str] = ["gpt-5.4-mini", "gpt-5.1"]

    # Reasoning effort per call. Classification is a trivial judgement;
    # extraction has to read a whole statement accurately; categorisation
    # needs to weigh several plausible accounts.
    effort_classify: str = "low"
    effort_extract: str = "medium"
    effort_categorise: str = "medium"

    # Set true to run the whole pipeline against the deterministic stub
    # provider, with no network and no key. Tests force this on.
    use_stub_llm: bool = False

    # Where the offline provider looks for the transcripts it replays. Only the
    # stub reads this; the real client never touches it.
    stub_replay_dir: Path = PROJECT_ROOT / "data" / "generated"

    llm_max_retries: int = 3
    llm_timeout_seconds: float = 180.0

    # ---- Storage ----
    # Defaulted so a machine matching it needs no DATABASE_URL line at all.
    # Anything else goes in .env; nothing else in this class has to.
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/aiacct"
    test_database_url: str | None = None

    # Echo every statement. Useful when a query is behaving unexpectedly.
    db_echo: bool = False

    upload_dir: Path = PROJECT_ROOT / "var" / "uploads"
    export_dir: Path = PROJECT_ROOT / "var" / "exports"

    # ---- Reference data ----
    chart_of_accounts_path: Path = DATA_DIR / "seeds" / "chart_of_accounts.sg.yaml"
    tax_codes_path: Path = DATA_DIR / "tax_codes.sg.yaml"
    confidence_config_path: Path = DATA_DIR / "confidence.yaml"

    # ---- Pipeline ----
    # A page with fewer extractable characters than this is treated as a scan
    # and goes down the vision path.
    digital_pdf_chars_per_page: int = 50

    # Bounded repair cycle. The validator decides to loop, never the model.
    max_extraction_attempts: int = 2

    # Shared prompt context dominates a categorisation call, so transactions
    # are batched. Larger batches lose per-item accuracy.
    categorisation_batch_size: int = 20

    # Few-shot examples of past corrections for this client.
    memory_example_count: int = 5

    # Payment must fall within this many days after an invoice date to match.
    document_match_max_days: int = 60
    document_match_min_score: float = 0.70

    max_upload_bytes: int = 50 * 1024 * 1024

    # ---- API ----
    api_key: str = "dev-local-key"
    api_title: str = "AI Accounting Assistant"

    def ensure_dirs(self) -> None:
        for path in (self.upload_dir, self.export_dir):
            path.mkdir(parents=True, exist_ok=True)

    @property
    def resolved_test_database_url(self) -> str:
        """Where tests run. Defaults to the main database with _test appended.

        A separate database rather than a schema, so a teardown bug cannot
        leave clutter sitting next to real data.
        """
        if self.test_database_url:
            return self.test_database_url
        from sqlalchemy.engine import make_url

        url = make_url(self.database_url)
        # render_as_string, not str(): SQLAlchemy masks the password as *** in
        # __str__ so that URLs are safe to log, which silently produces an
        # unusable connection string if you use it to connect.
        return url.set(database=f"{url.database}_test").render_as_string(
            hide_password=False
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
