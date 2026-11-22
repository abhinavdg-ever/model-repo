# Confidence levels: how High, Medium and Low are decided

Each page and each chart gets a confidence level (High / Medium / Low) and a confidence %.
High pages can be trusted as they are; Low pages should be looked at first.

## 1. Field confidence

Seven fields are scored on every page, the same seven the accuracy report measures:

| Field | Its confidence |
|---|---|
| Member Verification | the lower of Member Name and Member DOB |
| Date of Service (DOS) | its own |
| Blank / Junk | its own |
| Page Type | its own |
| Codability | its own |
| Provider Signature | its own (several signers: their average) |
| Orientation | its own |

A field's confidence comes from the extraction model's score for the value it chose. Each field
is then banded:

| Level | Confidence |
|---|---|
| **High** | 80% or more |
| **Medium** | 50% to under 80% |
| **Low** | under 50% |

A field with no value read on the page is not counted. On a Blank / Junk or duplicate page only
Blank / Junk and Codability are counted.

## 2. Page level

Like an NPS, from how many of its fields are High, Medium and Low:

**Page score** = (High fields - Low fields) / fields counted, from -100 to +100.
**Page confidence %** = (1 + score) / 2: all High 100%, all Medium 50%, all Low 0% (shown at the
20% floor). Its level is that % banded like any field: High 80% and above, Low under 50%.

## 3. Chart level

Like an NPS, from how many of its pages are High, Medium and Low:

**Score** = (High pages − Low pages) / pages scored, from −100 to +100 (a Medium page counts 0).

**Chart confidence %** = (1 + score) / 2: all pages High → 100%, all Medium → 50%, all Low → 0%
(shown as at least 20%, the floor every confidence has).

| Level | When |
|---|---|
| **High** | confidence 80% or more (score +60 or more) |
| **Medium** | 50% to 80% (score 0 to +60) |
| **Low** | under 50% (more Low pages than High) |

## Example

A 20-page chart: 17 pages High, 2 Medium, 1 Low. Score = (17 − 1) / 20 = +80, confidence 90%:
**High**. With 10 High, 8 Medium, 2 Low: score +40, 70%: **Medium**. With 2 High, 3 Medium, 5 Low:
score −30, 35%: **Low**.
