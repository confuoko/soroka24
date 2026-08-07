# Распознавание капчи: порталы судов иногда просят пройти проверку перед выдачей дела.
# Сейчас единственный движок — rucaptcha.com (см. captcha/rucaptcha.py).
from app.captcha.rucaptcha import (
    ATTEMPT_SOLVED,
    ATTEMPT_TIMEOUT,
    AttemptSink,
    CaptchaAttempt,
    CaptchaError,
    report_incorrect,
    solve_image,
)

__all__ = [
    "ATTEMPT_SOLVED",
    "ATTEMPT_TIMEOUT",
    "AttemptSink",
    "CaptchaAttempt",
    "CaptchaError",
    "report_incorrect",
    "solve_image",
]
