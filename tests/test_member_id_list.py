"""2026-11-01 (user): the Member ID shows every accepted, selected ID scored above 0.9 in
extraction.json, "Key: Value" one per line (review-ui extraction_review.member_id_rows)."""
from app.services.extraction_review import field_rows


def _row(key, value, score, model_score=None, accepted=True, selected=True):
    row = {"key": key, "value": value, "score": score, "accepted": accepted, "selected": selected}
    if model_score is not None:
        row["model_score"] = model_score
    return row


def _member_id(rows):
    page = {"fields": {"member_id": rows}}
    return next(f for f in field_rows(page) if f.id == "member_id").extracted


def test_every_sure_selected_id_one_per_line_model_score_first():
    rows = [
        _row("Policy/Group #", "G06330", 0.7033, 0.9814),
        _row("Insurance #", "A9000689900", 0.606, 0.984),
        _row("FIN #", "71yo", 0.92, 0.6287),  # rules sure, model not: left out
        _row("MRN", "555", 0.95, 0.99, selected=False),
        _row("ID", "777", 0.95, 0.99, accepted=False),
    ]
    assert _member_id(rows) == "Policy/Group #: G06330\nInsurance #: A9000689900"


def test_none_sure_shows_the_best_selected_one():
    rows = [_row("MRN", "123", 0.5, 0.6), _row("Acct #", "456", 0.4, 0.8)]
    assert _member_id(rows) == "Acct #: 456"


def test_rules_score_when_no_model_score():
    assert _member_id([_row("Chart #", "9001", 0.95)]) == "Chart #: 9001"
