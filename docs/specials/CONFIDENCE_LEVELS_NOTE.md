# Confidence levels: how High, Medium and Low are decided

Each page and each chart gets a confidence level (High / Medium / Low) and a confidence %.
High pages can be trusted as they are; Low pages should be looked at first.

## 1. Field confidence

Seven fields are scored on every page, the same seven the accuracy report measures:

| Field | Its confidence |
|---|---|
| Member Verification | the average of Member Name and Member DOB |
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

**Chart confidence %** = the average of its pages' levels, counting a High page 1.0, a Medium page 0.6
and a Low page 0. A critical page (Discharge Summary, Operative Note, Procedure Note) counts twice.

| Level | When (checked in this order) |
|---|---|
| **Low** | under 50%, or more than 15% of pages Low, or any critical page Low |
| **Medium** | under 80%, or any page Low, or more than 30% of pages Medium |
| **High** | 80% or more, no Low page, and 30% or fewer pages Medium |

## Example

A 100-page chart: 75 High, 25 Medium, 0 Low -> 90%: **High**. 82 High, 12 Medium, 6 Low -> 89.2%:
**Medium** (it has Low pages). 40 High, 15 Medium, 45 Low -> 49%: **Low**.
