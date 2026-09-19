---
name: humanize-code
description: Refactor code so it reads as if an experienced engineer wrote it, not a generator. Covers comment noise, naming, structure, error handling and defensive scaffolding — without changing behaviour. Use before handoff, review or client delivery.
---

# Humanize Code

Refactor for tone and density, not behaviour. Output should look like someone who knows the codebase wrote it quickly and well.

**Hard rule: never change what the code does.** No renamed public APIs, no altered signatures, no "while I'm here" fixes. If you spot a real bug, report it separately — don't silently fix it inside a style pass.

Renaming is the one place this rule bites. Rename freely inside a function. Rename module-level or exported names only after checking every call site, and never across a package boundary without saying so.

---

## Part 1 — Comments and noise

### 1.1 Comments that narrate the code

Delete any comment a competent reader could derive from the line below it.

```python
# Bad
# Loop through each user in the list
for user in users:
    # Check if the user is active
    if user.is_active:
        # Add the user to the results
        results.append(user)

# Good
for user in users:
    if user.is_active:
        results.append(user)
```

Keep comments that explain **why**, not **what** — a non-obvious constraint, a workaround, a business rule, a ticket reference.

```python
# Keep this kind
# Vendor API rejects batches >500 despite what their docs say
for chunk in batched(records, 500):
```

### 1.2 Banner and section comments

Delete outright. Structure should be evident from the code.

```python
# ============================================================
# HELPER FUNCTIONS
# ============================================================
```

### 1.3 Docstrings on everything

A docstring on `get_user_id()` saying "Gets the user ID" is noise. Keep them for public API surface, non-obvious parameters/units/return shapes, and real contracts (raises, side effects, ordering). Drop them from private helpers and anything under ~5 lines whose name already says it.

Where you keep one, one line is usually enough. Skip the full `Args:/Returns:/Raises:` block unless the project already does that everywhere.

### 1.4 Over-explained output

```python
# Bad
print("=" * 60)
print("Starting the data processing pipeline...")
print(f"✓ Successfully loaded {count} records from the database!")

# Good
log.info("loaded %d records", count)
```

No emoji, no decorative separators, no exclamation marks. Log lines are for whoever is debugging at 2am.

### 1.5 Vocabulary tells

Rewrite or cut: "Note that…", "It's important to…", "This function is responsible for…", "Here we…/Now we…", "comprehensive", "robust", "seamless", "leverage", and any docstring that restates the function name.

### 1.6 Leftover scaffolding

Delete `# TODO: implement` on implemented code, commented-out alternatives, `example_usage()` functions, demo `if __name__ == "__main__":` blocks in library modules, imports kept "just in case".

---

## Part 2 — Naming

Naming is the strongest tell. Generated names are grammatically correct and contextually deaf.

### 2.1 Don't encode the type in the name

The type system or the assignment already says it.

```python
# Bad                          # Good
user_list                      users
config_dict                    config
name_str                       name
count_int                      count
data_array                     rows
result_obj                     result
user_map                       users_by_id     # keep when it states the KEY
```

`users_by_id` survives because it says something the type doesn't. `user_list` doesn't.

### 2.2 Don't repeat the context

```python
# Bad
class User:
    user_id: int
    user_name: str
    user_email: str

    def get_user_email(self): ...

# Good
class User:
    id: int
    name: str
    email: str

    def email(self): ...
```

Same for modules: `auth/auth_helpers.py`, `user_service.UserService.user_create()`. Strip the echo.

### 2.3 Drop filler verbs

`process_`, `handle_`, `manage_`, `perform_`, `execute_`, `do_` almost always mean the author hadn't decided what the function does.

```python
# Bad                              # Good
process_data(rows)                 normalize(rows)
handle_user(u)                     deactivate(u)
manage_connection()                reconnect()
perform_validation(x)              validate(x)
do_cleanup()                       drop_stale_leases()
```

If you genuinely can't name it more precisely, the function is doing too much — but that's a design fix, not a style fix. Leave it and flag it.

### 2.4 Match length to scope

Short scope, short name. Long-lived or exported, spell it out.

```python
# Fine — the scope is three lines
for i, p in enumerate(pages):
    if p.blank:
        continue

# Not fine at module level
p = load_pipeline_config()      # → config
```

A loop variable named `current_item_being_processed` is a tell. So is a module-level `d`.

### 2.5 Use the domain's words, not generic ones

Read the codebase and the spec first. If the team says "chart", don't write `document`. If the schema says `dos_extraction_results`, don't name the variable `date_info`.

```python
# Bad                              # Good
date_info                          dos
doc_data                           chart
text_content                       ocr_text
check_result                       verdict
```

Domain vocabulary is the single clearest signal that a human who understands the problem wrote this.

### 2.6 Suffix classes honestly

`Manager`, `Helper`, `Util`, `Handler`, `Processor`, `Service` on a class that holds no state and has one method usually means it should be a function.

```python
# Bad
class DataProcessorHelper:
    def process(self, rows): ...

# Good
def normalize(rows): ...
```

Keep the suffix when it's a real pattern the codebase already uses (`UserRepository`, `PaymentGateway`) — consistency beats purity.

### 2.7 Booleans read as assertions

```python
# Bad                  # Good
flag                   is_final
status_check           has_signature
validation             rotation_applied
enable_retry_bool      retry_enabled
```

Prefix with `is_`, `has_`, `should_`, `can_` — or use a bare adjective/participle. Avoid negatives (`is_not_ready`, `disable_x`): they double-negate at the call site.

### 2.8 Be consistent, not creative

One concept, one word, everywhere. If it's `fetch` in one module, don't make it `retrieve`, `load`, `pull` and `get` in the next four. Pick whatever the codebase already leans on.

### 2.9 Files and modules

- Match the codebase's existing convention (`snake_case.py`, `kebab-case.ts`, `PascalCase.tsx`) — don't mix.
- Name for what's inside, not the layer: `member_verify.py` over `service_impl.py`.
- No `utils.py` / `helpers.py` / `common.py` dumping grounds if you can avoid adding to one. If you must, put the function next to its only caller instead.
- `misc`, `stuff`, `temp`, `new_`, `v2_`, `final_` in a filename are all tells.

### 2.10 Abbreviations

Use the ones the domain already uses (`ocr`, `dos`, `ner`, `id`, `url`, `db`). Don't invent new ones (`usr`, `cfg`, `proc`, `val`, `tmp_res`). Don't expand established ones into prose (`optical_character_recognition_text`).

---

## Part 3 — Structure and control flow

### 3.1 Defensive code for impossible states

```python
# Bad
def process(items):
    if items is None:
        return []
    if not isinstance(items, list):
        raise TypeError("items must be a list")
    if len(items) == 0:
        return []
    ...

# Good
def process(items):
    ...
```

Keep validation at real trust boundaries — user input, network responses, file contents, anything crossing a public API. Drop it between your own functions.

### 3.2 Try/except that adds nothing

Catching, logging and re-raising the same exception is noise. Let it propagate.

```python
# Bad
try:
    result = fetch(url)
except Exception as e:
    logger.error(f"Error fetching: {e}")
    raise

# Good
result = fetch(url)
```

Catch when you can do something about it: a fallback, a retry, a clearer domain exception, or a genuinely useful context line. Never catch bare `Exception` to "be safe".

### 3.3 Ceremony that adds nothing

Constants extracted for a value used once. Wrappers that call one other function. Variables assigned then immediately returned. Two-member enums used in one place. Type aliases used once.

```python
# Bad
DEFAULT_TIMEOUT_SECONDS = 30

def fetch_data(url):
    response = make_request(url, timeout=DEFAULT_TIMEOUT_SECONDS)
    result = response.json()
    return result

# Good
def fetch_data(url):
    return make_request(url, timeout=30).json()
```

Keep the constant if the value appears in several places or needs a name to be understood.

### 3.4 Uniform structure

Generated code makes every function the same shape and length. Real code is uneven — a three-line function beside a forty-line one is normal. Don't split a coherent function to hit a size target, don't pad a short one.

### 3.5 Guard clauses over nesting

```python
# Bad
def handle(page):
    if page is not None:
        if page.text:
            if not page.blank:
                return extract(page.text)
    return None

# Good
def handle(page):
    if page.blank or not page.text:
        return None
    return extract(page.text)
```

### 3.6 Redundant annotations

Annotate signatures. Skip locals where the type is obvious from the assignment.

```python
# Bad                       # Good
count: int = 0              count = 0
names: list[str] = []       names = []
user: User = get_user(id)   user = get_user(id)
```

---

## Part 4 — Match the surrounding code

Before changing anything, read 2–3 neighbouring files and match what's there:

- Comment density and style
- Naming conventions and the abbreviations the team actually uses
- Error-handling approach
- Whether the project leans on docstrings, type hints, logging vs print
- Import ordering and grouping
- Test naming and layout

A file consistent with its codebase reads as written by that team. One that's "better" but different reads as foreign. Where this skill and the local convention disagree, **the local convention wins** — note the conflict rather than silently overriding it.

---

## Procedure

1. Read nearby files first to establish local style.
2. Work file by file — don't batch-rewrite a repo in one pass.
3. Strip noise, then fix naming, then structure. Naming changes touch call sites, so do them deliberately and check references.
4. Re-read as if reviewing a colleague's PR. Anything that makes you ask "why is this here?" goes.
5. Run the tests. If there are none, diff carefully and say so.
6. Report what changed by category, and flag anything left alone because you weren't sure it was safe.

## What not to do

- Don't introduce deliberate sloppiness — inconsistent spacing, typos, dead code. Bad code doesn't read as human, it reads as bad.
- Don't strip comments encoding knowledge not recoverable from the code.
- Don't remove validation at trust boundaries.
- Don't rename across a module boundary without checking every call site.
- Don't reformat wholesale — a diff touching every line is worse than the problem.
- Don't make test files terser. Tests are allowed to be explicit and repetitive.
