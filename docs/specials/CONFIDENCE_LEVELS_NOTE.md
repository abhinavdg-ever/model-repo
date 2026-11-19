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

| Level | When |
|---|---|
| **High** | every field is High |
| **Low** | any field is Low |
| **Medium** | otherwise (no field Low, at least one Medium) |

**Page confidence %** = the average of its fields' confidences, never above what its level allows:
a Medium page shows at most 79%, a Low page at most 49%.

## 3. Chart level

| Level | When |
|---|---|
| **High** | every page is High |
| **Low** | 10% or more of the pages are Low |
| **Medium** | otherwise |

**Chart confidence %** = the average of its pages' confidence %, with the same limits: a Medium
chart shows at most 79%, a Low chart at most 49%.

## Example

A 20-page chart: 17 pages High, 2 Medium, 1 Low. 1 of 20 pages (5%) is Low, under 10%, and not
every page is High, so the chart is **Medium**, and its % is at most 79%.
