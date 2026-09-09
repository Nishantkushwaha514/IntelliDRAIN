def compute_risk_score(blockage_ratio: float, rainfall_mm: float) -> float:
    """
    The core IntelliDRAIN risk formula.
    blockage_ratio: 0.0 to 1.0 (how blocked the drain is)
    rainfall_mm: rainfall in the last hour, in millimeters
    Returns a risk score from 0.0 to 1.0.
    """
    rainfall_intensity = max(0.0, min(rainfall_mm / 50.0, 1.0))  # 50mm/hr = extreme flash-flood cap

    alpha = 1.2  # blockage matters slightly more than rainfall
    beta = 1.0

    risk_score = 1.0 - ((1.0 - blockage_ratio) ** alpha * (1.0 - rainfall_intensity) ** beta)
    return round(max(0.0, min(risk_score, 1.0)), 3)


def classify_risk(risk_score: float) -> tuple[str, str]:
    """
    Turns a raw score into the label + recommended action the app displays.
    Returns (risk_level, dispatch_action).
    """
    if risk_score >= 0.75:
        return "CRITICAL", "Immediate dispatch: High probability of localized structural overflow."
    elif risk_score >= 0.45:
        return "ELEVATED", "Monitored status: Secondary verification advised."
    else:
        return "LOW", "Routine maintenance cycle clear."
