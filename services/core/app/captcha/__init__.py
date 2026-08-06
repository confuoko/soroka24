# Распознавание капчи: порталы судов иногда просят пройти проверку перед выдачей дела.
# Сейчас единственный движок — rucaptcha.com (см. captcha/rucaptcha.py).
from app.captcha.rucaptcha import CaptchaError, report_incorrect, solve_image

__all__ = ["CaptchaError", "report_incorrect", "solve_image"]
