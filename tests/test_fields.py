"""The field catalogue, and the override that makes the Settings page real.

Two claims are worth pinning and neither is arithmetic.

The first is that the catalogue and the Log's prompt cannot drift. They are one
string read from one place, and the moment they become two the page starts
describing a prompt the model is not being given — which is the worst kind of
documentation, because it looks maintained.

The second is that an edited definition actually reaches the model. A settings
page whose edits do nothing is worse than no settings page: it invites you to
tune something that isn't listening, and you would only find out by noticing
the model never changed its behaviour.

Run: python3 tests/test_fields.py
"""
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

_TMP = tempfile.mkdtemp(prefix="jobsearch-fields-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMP, "test.db")
os.environ.pop("APP_PASSWORD", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

from app import fields, logspec  # noqa: E402

failures = []


def check(name, cond, detail=""):
    if cond:
        print("OK   {}".format(name))
    else:
        failures.append(name)
        print("FAIL {}{}".format(name, ": " + detail if detail else ""))


# --------------------------------------------------------------------------- #
# The catalogue is complete and internally consistent
# --------------------------------------------------------------------------- #
seen = [r["field"] for r in fields.CATALOGUE]
check("no field is catalogued twice", len(seen) == len(set(seen)))

for row in fields.CATALOGUE:
    for key in ("field", "label", "group", "writer", "definition"):
        check("{} has a {}".format(row["field"], key),
              bool(str(row.get(key, "")).strip()))
    check("{}'s writer is one of the four".format(row["field"]),
          row["writer"] in fields.WRITERS)
    check("{}'s definition is a sentence, not a restatement of its name"
          .format(row["field"]), len(row["definition"]) > 40)

# The load-bearing one. A field the Log can write with no catalogue entry
# ships as a bare name for the model to guess at, which is the exact failure
# that put "comp is light" under `pain` instead of `risks`.
for field in logspec.WRITABLE:
    check("the Log-writable field '{}' is in the catalogue".format(field),
          field in fields.BY_FIELD)
    check("...and is marked as written by the Log",
          fields.BY_FIELD.get(field, {}).get("writer") == "log")

# And the reverse: nothing claims to be Log-writable that isn't.
for row in fields.CATALOGUE:
    if row["writer"] == "log":
        check("'{}' says the Log writes it and logspec agrees"
              .format(row["field"]), row["field"] in logspec.WRITABLE)

check("the prompt's definitions come from the catalogue",
      logspec.DEFINITIONS == fields.defaults_for(logspec.WRITABLE))
check("...and cover every writable field",
      sorted(logspec.DEFINITIONS) == sorted(logspec.WRITABLE))

# The three deliberate exclusions are documented rather than merely absent.
for excluded in ("champion", "manual_forecast", "seniority", "speciality"):
    check("'{}' is catalogued even though the Log cannot write it"
          .format(excluded), excluded in fields.BY_FIELD)
    check("...and is not marked as Log-writable",
          fields.BY_FIELD[excluded]["writer"] != "log")
check("champion's definition explains why automation is kept off it",
      "automation" in fields.BY_FIELD["champion"]["definition"])


# --------------------------------------------------------------------------- #
# rows(): what the page renders
# --------------------------------------------------------------------------- #
plain = fields.rows(writable=logspec.WRITABLE)
check("every catalogue entry becomes a row", len(plain) == len(fields.CATALOGUE))
check("nothing is marked edited when there are no overrides",
      not any(r["edited"] for r in plain))
check("the Log-writable rows are marked as reaching the prompt",
      sum(1 for r in plain if r["in_prompt"]) == len(logspec.WRITABLE))
check("a field the Log cannot write is not marked as reaching the prompt",
      not next(r for r in plain if r["field"] == "champion")["in_prompt"])

edited = fields.rows(overrides={"pain": "My own words for pain."},
                     writable=logspec.WRITABLE)
row = next(r for r in edited if r["field"] == "pain")
check("an override replaces the definition", row["definition"] == "My own words for pain.")
check("...is flagged as yours", row["edited"] is True)
check("...and keeps the shipped wording alongside, so a reset has something "
      "to reset to", row["default"] == fields.BY_FIELD["pain"]["definition"])

blank = fields.rows(overrides={"pain": "   "}, writable=logspec.WRITABLE)
row = next(r for r in blank if r["field"] == "pain")
check("a whitespace-only override is ignored rather than blanking the field",
      row["definition"] == fields.BY_FIELD["pain"]["definition"]
      and row["edited"] is False)

check("groups are returned in catalogue order",
      fields.groups()[0] == fields.CATALOGUE[0]["group"])


# --------------------------------------------------------------------------- #
# An override actually reaches the model
# --------------------------------------------------------------------------- #
STAGES, CATS = ["Discovery"], ["Other"]
base = logspec.system_prompt(stages=STAGES, categories=CATS, today="2026-09-06")
check("the shipped definition is in the prompt by default",
      fields.BY_FIELD["pain"]["definition"][:40] in base)

tuned = logspec.system_prompt(stages=STAGES, categories=CATS, today="2026-09-06",
                              definitions={"pain": "Their hiring problem only."})
check("an override reaches the prompt", "Their hiring problem only." in tuned)
check("...and displaces the shipped wording",
      fields.BY_FIELD["pain"]["definition"][:40] not in tuned)

untouched = logspec.system_prompt(stages=STAGES, categories=CATS,
                                  today="2026-09-06",
                                  definitions={"pain": "Their problem."})
check("a field you did not override keeps its default",
      fields.BY_FIELD["risks"]["definition"][:40] in untouched)

ignored = logspec.system_prompt(stages=STAGES, categories=CATS,
                                today="2026-09-06",
                                definitions={"pain": "   "})
check("a blank override cannot delete a field's meaning from the prompt",
      fields.BY_FIELD["pain"]["definition"][:40] in ignored)

sneaky = logspec.system_prompt(stages=STAGES, categories=CATS,
                               today="2026-09-06",
                               definitions={"champion": "Anyone friendly."})
check("an override for a field the Log cannot write never reaches the prompt",
      "Anyone friendly." not in sneaky)


print()
if failures:
    print("{} FAILED: {}".format(len(failures), ", ".join(failures)))
    sys.exit(1)
print("all checks passed")
