"""Generate and validate Maestro YAML from Jira steps-to-reproduce (STR)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from utils.llm import ask_for_json
from utils.secrets import redact

DEFAULT_APP_ID = "de.payzy.pro.pre_prod"
OTP_VALUE = "000000"  # DE ODS pre-prod verify-with-otp expects 6 digits

ALLOWED_COMMANDS = frozenset(
    {
        "tapOn",
        "doubleTapOn",
        "longPressOn",
        "inputText",
        "eraseText",
        "assertVisible",
        "assertNotVisible",
        "scroll",
        "scrollUntilVisible",
        "swipe",
        "waitForAnimationToEnd",
        "extendedWaitUntil",
        "pressKey",
        "hideKeyboard",
        "copyTextFrom",
        "pasteText",
        "repeat",
        "runFlow",
        "launchApp",
    }
)

DENIED_COMMANDS = frozenset({"evalScript", "runScript", "openLink", "setLocation", "airplaneMode"})

_PLACEHOLDER_REPRO = re.compile(
    r"reproduce using the steps in the linked jira|see jira|n/?a|none|unknown",
    re.IGNORECASE,
)


@dataclass
class MaestroFlowGenResult:
    success: bool
    flow_path: Path | None = None
    yaml_text: str = ""
    steps: list[Any] = field(default_factory=list)
    failure_type: str | None = None  # "unverified" when STR unclear
    error: str | None = None


def str_is_clear(reproduction_steps: list[str] | None, description: str = "") -> bool:
    """True when STR looks specific enough to author Maestro steps."""
    steps = [str(s).strip() for s in (reproduction_steps or []) if str(s).strip()]
    if not steps:
        return False
    if len(steps) == 1 and _PLACEHOLDER_REPRO.search(steps[0]):
        return False
    joined = " ".join(steps)
    if len(joined) < 12:
        return False
    # Require at least one actionable cue
    cues = ("tap", "click", "enter", "open", "navigate", "select", "press", "type", "scroll", "go to", "login")
    blob = (joined + " " + description).lower()
    return any(c in blob for c in cues) or len(steps) >= 2


_BUSINESS_OWNER_RE = re.compile(r"business\s*owner", re.IGNORECASE)
_PASSWORD_RECOVERY_RE = re.compile(
    r"forgot\s*password|password\s*recovery|reset\s*password|"
    r"new\s*password|create\s*new\s*password|passwort\s*vergessen",
    re.IGNORECASE,
)


def mentions_business_owner(*texts: str) -> bool:
    """True when Jira summary/description/STR refers to a Business Owner path."""
    blob = " ".join(t for t in texts if t)
    return bool(_BUSINESS_OWNER_RE.search(blob))


def mentions_password_recovery(*texts: str) -> bool:
    """True when the ticket is about forgot/reset password (blank password page, etc.)."""
    blob = " ".join(t for t in texts if t)
    return bool(_PASSWORD_RECOVERY_RE.search(blob))


def business_owner_onboarding_steps() -> list[dict[str, Any]]:
    """Fixed prefix: Launch App → Business Owner → Intro screens → Login page.

    Prefer Compose resource-ids (stable across EN/DE copy). Splash can take
    ~10–20s after launchApp before the role card is in the hierarchy.
    """
    role_id = "login__role_selection__business_owner_card"
    next_cta = "Next|Weiter"
    login_cta = "Login|Login/register|Anmelden|Einloggen"
    return [
        {
            "extendedWaitUntil": {
                "visible": {"id": role_id},
                "timeout": 45000,
            }
        },
        {"tapOn": {"id": role_id}},
        {"extendedWaitUntil": {"visible": f"{next_cta}|{login_cta}", "timeout": 20000}},
        {
            "repeat": {
                "while": {"visible": next_cta},
                "commands": [
                    {"tapOn": next_cta},
                    {"waitForAnimationToEnd": {"timeout": 2000}},
                ],
            }
        },
        {"tapOn": login_cta},
        {"waitForAnimationToEnd": {"timeout": 3000}},
    ]


def password_recovery_steps(
    email: str,
    otp: str = OTP_VALUE,
    *,
    business_owner: bool = True,
) -> list[dict[str, Any]]:
    """Device-proven path: BO → Login → Forgot password → email → OTP → Create new password.

    Final asserts expect New/Confirm password fields (the WFDR blank-page defect).
    Pre-prod accepts OTP ``000000`` for automation.
    """
    steps: list[dict[str, Any]] = [{"launchApp": {"clearState": True}}]
    if business_owner:
        steps.extend(business_owner_onboarding_steps())
    steps.extend(
        [
            {
                "extendedWaitUntil": {
                    "visible": {"id": "login__login__recover_password"},
                    "timeout": 30000,
                }
            },
            {"tapOn": {"id": "login__login__recover_password"}},
            {
                "extendedWaitUntil": {
                    "visible": {"id": "login__password_recovery__email_address_input_field"},
                    "timeout": 20000,
                }
            },
            {"tapOn": {"id": "login__password_recovery__email_address_input_field"}},
            {"inputText": email},
            {"tapOn": {"id": "ods_payzy__action_button_slot__primary_button"}},
            {
                "extendedWaitUntil": {
                    "visible": {"id": "login__verify_with_otp__code_input"},
                    "timeout": 30000,
                }
            },
            {"tapOn": {"id": "login__verify_with_otp__code_input"}},
            {"inputText": otp},
            {"waitForAnimationToEnd": {"timeout": 8000}},
            {
                "extendedWaitUntil": {
                    "visible": "Create new password|Neues Passwort",
                    "timeout": 20000,
                }
            },
            {"assertVisible": "Create new password|Neues Passwort"},
            {
                "assertVisible": "New password|Confirm password|Confirm new password|Password"
            },
        ]
    )
    return steps


def login_prefix_steps(
    email: str,
    password: str,
    otp: str = OTP_VALUE,
    *,
    business_owner: bool = False,
) -> list[dict[str, Any]]:
    """Fixed Maestro login/OTP prefix (selectors are heuristics; adjust on device if needed).

    When ``business_owner`` is True (Jira mentions Business Owner), prepend
    role selection + intro carousel before the email/password/OTP fields.
    """
    steps: list[dict[str, Any]] = [
        {"launchApp": {"clearState": True}},
    ]
    if business_owner:
        steps.extend(business_owner_onboarding_steps())
    email_id = "login__login__email_address_input_field"
    password_id = "login__login__password_input_field"
    login_id = "login__login__login_cta"
    steps.extend(
        [
            {"extendedWaitUntil": {"visible": {"id": email_id}, "timeout": 30000}},
            {"tapOn": {"id": email_id}},
            {"inputText": email},
            {"tapOn": {"id": password_id}},
            {"inputText": password},
            {"tapOn": {"id": login_id}},
            {
                "extendedWaitUntil": {
                    "visible": {"id": "login__verify_with_otp__code_input"},
                    "timeout": 30000,
                }
            },
            {"tapOn": {"id": "login__verify_with_otp__code_input"}},
            {"inputText": otp},
            {"waitForAnimationToEnd": {"timeout": 8000}},
        ]
    )
    return steps


def validate_maestro_steps(steps: list[Any]) -> tuple[list[Any], list[str]]:
    """Keep only allowlisted commands; return (clean_steps, errors)."""
    cleaned: list[Any] = []
    errors: list[str] = []
    for i, step in enumerate(steps):
        if isinstance(step, str):
            cmd = step.strip()
            if cmd in DENIED_COMMANDS:
                errors.append(f"Step {i}: denied command '{cmd}'")
                continue
            if cmd not in ALLOWED_COMMANDS:
                errors.append(f"Step {i}: unknown command '{cmd}'")
                continue
            cleaned.append(cmd)
            continue
        if not isinstance(step, dict) or len(step) != 1:
            errors.append(f"Step {i}: expected a single-key mapping, got {type(step).__name__}")
            continue
        cmd = next(iter(step))
        if cmd in DENIED_COMMANDS:
            errors.append(f"Step {i}: denied command '{cmd}'")
            continue
        if cmd not in ALLOWED_COMMANDS:
            errors.append(f"Step {i}: unknown command '{cmd}'")
            continue
        if cmd == "runFlow":
            val = step[cmd]
            # Only allow local relative flow names, not URLs/absolute escapes
            if isinstance(val, dict):
                file_val = str(val.get("file") or "")
            else:
                file_val = str(val)
            if "://" in file_val or file_val.startswith("/") or ".." in file_val:
                errors.append(f"Step {i}: runFlow path not allowed: {file_val!r}")
                continue
        cleaned.append(step)
    return cleaned, errors


def build_yaml(app_id: str, steps: list[Any]) -> str:
    header = f"appId: {app_id}\n---\n"
    body = yaml.safe_dump(steps, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return header + body


def flow_output_path(orchestrator_root: Path, ticket_id: str) -> Path:
    return orchestrator_root / "local" / "runs" / ticket_id / "repro.yaml"


def generate_str_steps_via_llm(
    reproduction_steps: list[str],
    *,
    description: str = "",
    app_id: str = DEFAULT_APP_ID,
    cwd: Path | None = None,
    timeout: float = 90.0,
) -> tuple[list[Any] | None, str | None]:
    """Ask LLM for Maestro STR steps only (no login). Returns (steps, error)."""
    repro_text = "\n".join(f"- {s}" for s in reproduction_steps)
    prompt = (
        "You convert Jira steps-to-reproduce into Maestro mobile UI test steps.\n"
        f"appId is {app_id}. Do NOT invent launchApp, Business Owner role selection, "
        "intro-screen Next/Login, or email/password/OTP steps (those are added separately).\n"
        "Do NOT invent steps that are not clearly implied by the STR.\n"
        "Allowed commands only: "
        + ", ".join(sorted(ALLOWED_COMMANDS - {'launchApp'}))
        + ".\n"
        "Never use evalScript, runScript, openLink, or shell.\n"
        "Respond with ONLY JSON: {\"steps\": [ ... Maestro step objects ... ], \"clear\": true/false}.\n"
        "If the STR is too vague to automate, set clear=false and steps=[].\n\n"
        f"Description:\n{redact(description)}\n\n"
        f"Steps to reproduce:\n{redact(repro_text)}\n"
    )
    result = ask_for_json(prompt, cwd=cwd, timeout=timeout)
    if not result.success or not isinstance(result.data, dict):
        return None, result.error or "LLM did not return Maestro steps JSON"
    if result.data.get("clear") is False:
        return None, "STR marked unclear by model"
    raw_steps = result.data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        return None, "No Maestro steps returned"
    cleaned, errors = validate_maestro_steps(raw_steps)
    if errors and not cleaned:
        return None, "; ".join(errors)
    return cleaned, None


def generate_and_save_flow(
    ticket_id: str,
    reproduction_steps: list[str],
    *,
    orchestrator_root: Path,
    description: str = "",
    app_id: str = DEFAULT_APP_ID,
    email: str | None = None,
    password: str | None = None,
    use_llm: bool = True,
    cwd: Path | None = None,
) -> MaestroFlowGenResult:
    """Build login+STR Maestro YAML under local/runs/<TICKET>/repro.yaml."""
    if not str_is_clear(reproduction_steps, description):
        return MaestroFlowGenResult(
            success=False,
            failure_type="unverified",
            error="Reproduction steps are missing or too unclear to generate a Maestro flow.",
        )

    email = email if email is not None else os.environ.get("MAESTRO_EMAIL", "")
    password = password if password is not None else os.environ.get("MAESTRO_PASSWORD", "")
    if not email:
        return MaestroFlowGenResult(
            success=False,
            failure_type="infrastructure",
            error="MAESTRO_EMAIL must be set to generate the Maestro flow.",
        )

    use_bo = (
        mentions_business_owner(description, *reproduction_steps)
        or app_id.startswith("de.payzy.pro")
        or app_id.startswith("gr.payzy.pro")
    )
    use_recovery = mentions_password_recovery(description, *reproduction_steps)

    if use_recovery:
        # Device-proven forgot-password path; STR asserts are embedded.
        all_steps = password_recovery_steps(email, business_owner=use_bo)
    else:
        if not password:
            return MaestroFlowGenResult(
                success=False,
                failure_type="infrastructure",
                error="MAESTRO_PASSWORD must be set to generate the login prefix.",
            )
        str_steps: list[Any] = []
        if use_llm:
            str_steps, err = generate_str_steps_via_llm(
                reproduction_steps, description=description, app_id=app_id, cwd=cwd
            )
            if err or not str_steps:
                return MaestroFlowGenResult(
                    success=False,
                    failure_type="unverified",
                    error=err or "Could not derive Maestro steps from STR.",
                )
        else:
            for step in reproduction_steps:
                text = str(step).strip()
                if text:
                    str_steps.append({"assertVisible": text[:80]})

        cleaned, val_errors = validate_maestro_steps(str_steps)
        if not cleaned:
            return MaestroFlowGenResult(
                success=False,
                failure_type="unverified",
                error="; ".join(val_errors) or "No valid Maestro steps after validation.",
            )
        all_steps = login_prefix_steps(email, password, business_owner=use_bo) + cleaned

    yaml_text = build_yaml(app_id, all_steps)
    # Redact credentials from any accidental echo in stored copy used for logs —
    # the on-disk flow must contain real credentials for Maestro; redact only error paths.
    out = flow_output_path(orchestrator_root, ticket_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml_text, encoding="utf-8")
    return MaestroFlowGenResult(
        success=True,
        flow_path=out,
        yaml_text=yaml_text,
        steps=all_steps,
    )
