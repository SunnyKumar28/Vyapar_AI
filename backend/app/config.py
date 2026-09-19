"""Central configuration. Every tunable that affects a demo number lives here or in the scenario JSON."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv() -> None:
    """Load the local, gitignored .env without overriding shell variables."""
    path = ROOT / ".env"
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()

_DEFAULT_PROVIDER = os.getenv(
    "VYAPAAR_LLM", "sarvam" if os.getenv("SARVAM_API_KEY") else "scripted"
).strip().lower()
_DEFAULT_MODEL = os.getenv(
    "VYAPAAR_MODEL",
    "sarvam-105b-conversations" if _DEFAULT_PROVIDER == "sarvam" else "claude-opus-5",
)


def _default_db_path() -> Path:
    """Use writable temporary storage when the FastAPI app runs as a Vercel Function."""
    configured = os.getenv("VYAPAAR_DB", "").strip()
    if configured:
        return Path(configured)
    if os.getenv("VERCEL"):
        return Path("/tmp/vyapaar.db")
    return ROOT / "vyapaar.db"

# Shared by chat, Saaras STT and Bulbul TTS. Bulbul v3 currently accepts these
# BCP-47 codes; keeping the allow-list in code prevents invalid API values.
SARVAM_LANGUAGES: dict[str, str] = {
    "en-IN": "English",
    "hi-IN": "Hindi",
    "bn-IN": "Bengali",
    "ta-IN": "Tamil",
    "te-IN": "Telugu",
    "kn-IN": "Kannada",
    "ml-IN": "Malayalam",
    "mr-IN": "Marathi",
    "gu-IN": "Gujarati",
    "pa-IN": "Punjabi",
    "od-IN": "Odia",
}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class PolicyConfig:
    """§15 policy engine. Enforced in code before any spend/contact action — never in a prompt."""

    spend_cap_paise_per_run: int = 150_000          # ₹1,500 default hard cap per run
    spend_cap_paise_per_week: int = 500_000         # ₹5,000 per merchant per week
    campaigns_per_week: int = 2                     # frequency cap
    audience_ceiling: int = 1_000
    quiet_hours_start: time = time(21, 30)
    quiet_hours_end: time = time(8, 0)


@dataclass(frozen=True)
class ImpactConfig:
    """Parameters of `heuristic_winback_v1`. The ONLY source of ₹ projections (§4.2)."""

    redemption_rate_low: float = 0.100
    redemption_rate_high: float = 0.135
    avg_incremental_ticket_paise: int = 22_600      # ₹226
    whatsapp_cost_paise_per_message: int = 200      # blended demo rate: template + BSP + creative
    cost_cap_rounding_paise: int = 10_000           # quote the cost cap rounded UP to ₹100
    impact_rounding_paise: int = 10_000             # quote impact rounded to nearest ₹100


@dataclass(frozen=True)
class AgentConfig:
    max_steps: int = 8
    tool_timeout_s: float = 2.0
    tool_retries: int = 2
    max_tokens: int = 4_096
    model: str = _DEFAULT_MODEL
    # "scripted" = deterministic planner, zero deps, always works offline (demo default).
    # "sarvam" / "claude" = real function-calling loop against the selected provider.
    provider: str = _DEFAULT_PROVIDER


@dataclass(frozen=True)
class Settings:
    db_path: Path = _default_db_path()
    seed: int = _int("VYAPAAR_SEED", 20260917)
    pii_secret: str = os.getenv("VYAPAAR_PII_SECRET", "dev-only-tokenization-key")
    approval_secret: str = os.getenv("VYAPAAR_APPROVAL_SECRET", "dev-only-approval-key")
    sarvam_api_key: str = os.getenv("SARVAM_API_KEY", "").strip()
    sarvam_base_url: str = os.getenv("SARVAM_BASE_URL", "https://api.sarvam.ai").strip()
    sarvam_stt_model: str = os.getenv("SARVAM_STT_MODEL", "saaras:v3").strip()
    sarvam_tts_model: str = os.getenv("SARVAM_TTS_MODEL", "bulbul:v3").strip()
    sarvam_language: str = os.getenv("SARVAM_LANGUAGE", "hi-IN").strip()
    sarvam_speaker: str = os.getenv("SARVAM_SPEAKER", "shubh").strip()
    sarvam_timeout_s: float = float(os.getenv("SARVAM_TIMEOUT_S", "30"))
    demo_merchant_id: int = _int("VYAPAAR_DEMO_MERCHANT", 1042)
    lapsed_gap_days: int = 45
    regular_min_visits: int = 3                     # a "regular" has >=3 lifetime visits (excludes walk-ins)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    impact: ImpactConfig = field(default_factory=ImpactConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)

    @property
    def scenario_path(self) -> Path:
        return ROOT / "datagen" / "scenarios" / "sharma_tea_stall.json"

    @property
    def dashboard_dir(self) -> Path:
        return ROOT / "dashboard"

    @property
    def playbook_dir(self) -> Path:
        return Path(__file__).resolve().parent / "playbooks" / "definitions"


settings = Settings()
