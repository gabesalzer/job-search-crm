"""Both classifiers: the prompts, and every way a reply can fail to parse.

Stdlib only, against the real `app/classify.py`. The refusal paths matter more
than the happy one here. A classification is a small, plausible-looking value
that nothing downstream can tell apart from a correct one, so the parser
writing nothing is the only thing standing between a garbled reply and a field
that silently poisons every later comparison built on it.

Run: python3 tests/test_classify.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import classify  # noqa: E402

failures = []


def check(name, cond, detail=""):
    if cond:
        print("OK   {}".format(name))
    else:
        failures.append(name)
        print("FAIL {}{}".format(name, ": " + detail if detail else ""))


# --------------------------------------------------------------------------- #
# The prompts are generated from the vocabularies, so they cannot drift
# --------------------------------------------------------------------------- #
posting_prompt = classify.posting_system_prompt()
company_prompt = classify.company_system_prompt()

for value in classify.SENIORITY_VALUES + classify.SPECIALITY_VALUES:
    check("the posting prompt offers '{}'".format(value), value in posting_prompt)
for value in classify.FUNDING_STAGES + classify.EMPLOYEE_BANDS:
    check("the company prompt offers '{}'".format(value), value in company_prompt)

check("both prompts name the decline token",
      classify.DECLINE in posting_prompt and classify.DECLINE in company_prompt)
check("both prompts state the packet is data, not instructions",
      "not instructions" in posting_prompt and "not instructions" in company_prompt)
check("the company prompt forbids answering from memory",
      "Do not use them" in company_prompt,
      "a recalled funding round is stale and uncheckable, and would be stored "
      "beside a URL it did not come from")

# --------------------------------------------------------------------------- #
# Posting packets
# --------------------------------------------------------------------------- #
packet = classify.build_posting_packet(
    title="VP, Revenue Operations", company="Condor", location="Remote",
    jd_text="Own forecasting, territory design and the Salesforce estate.")
check("the packet is fenced",
      packet.startswith("<job_posting>") and packet.endswith("</job_posting>"),
      "so the description cannot be read as instructions")
check("the packet carries the title", "VP, Revenue Operations" in packet)
check("the packet carries the description", "territory design" in packet)

thin = classify.build_posting_packet(title="Ops Lead")
check("a packet with only a title still renders", "Ops Lead" in thin)
check("an absent field is omitted rather than sent as None",
      "None" not in thin)

huge = classify.build_posting_packet(jd_text="x" * (classify.MAX_JD_CHARS + 5000))
check("an enormous description is clipped", len(huge) < classify.MAX_JD_CHARS + 500)
check("and the clipping is announced", "truncated" in huge)

# --------------------------------------------------------------------------- #
# Posting replies
# --------------------------------------------------------------------------- #
sen, spec, note, ok = classify.parse_posting_reply(
    "SENIORITY: Director+\nSPECIALITY: Systems + Strategy\nREASON: owns the function.")
check("a clean reply parses", (sen, spec, ok) == ("Director+", "Systems + Strategy", True))
check("the reason is kept", note == "owns the function.")

sen, spec, note, ok = classify.parse_posting_reply(
    "SENIORITY: NONE\nSPECIALITY: Systems\nREASON: an IC analyst role.")
check("a declined field is a real answer, not a failure",
      (sen, spec, ok) == (None, "Systems", True),
      "an IC posting is neither Director+ nor Manager and must not be rounded")

sen, spec, note, ok = classify.parse_posting_reply(
    "SENIORITY: NONE\nSPECIALITY: NONE\nREASON: too vague to place.")
check("declining both is still a completed read", ok is True and note is not None)

check("a half-parsed reply writes nothing at all",
      classify.parse_posting_reply("SENIORITY: fairly senior\nSPECIALITY: Systems")
      == (None, None, None, False),
      "the two fields are independent judgments; a reply that lost the shape "
      "on one line has not earned trust on the other")

check("a missing line writes nothing",
      classify.parse_posting_reply("SENIORITY: Manager")[3] is False)
check("prose instead of a reply writes nothing",
      classify.parse_posting_reply("This looks like a senior role to me.")[3] is False)
check("an empty reply writes nothing",
      classify.parse_posting_reply("")[3] is False)

check("a value outside the picklist is refused, not coerced",
      classify.parse_posting_reply(
          "SENIORITY: Senior Manager\nSPECIALITY: Systems")[3] is False,
      "'Senior Manager' is not 'Manager', and a matcher loose enough to accept "
      "it would put a value in the field the model did not choose")

check("case and trailing punctuation are tolerated",
      classify.parse_posting_reply(
          "SENIORITY: director+.\nSPECIALITY: strategy\nREASON: x")[:2]
      == ("Director+", "Strategy"),
      "canonical casing is stored, not whatever came back")

check("lines in a different order still parse",
      classify.parse_posting_reply(
          "REASON: x\nSPECIALITY: Systems\nSENIORITY: Manager")[:2]
      == ("Manager", "Systems"))

check("a reply wrapped in chatter still parses",
      classify.parse_posting_reply(
          "Sure!\nSENIORITY: Manager\nSPECIALITY: Systems\nREASON: x\nHope that helps.")[3]
      is True)

long_reason = classify.parse_posting_reply(
    "SENIORITY: Manager\nSPECIALITY: Systems\nREASON: " + "y" * 900)[2]
check("a runaway reason is capped", len(long_reason) <= 400)

# --------------------------------------------------------------------------- #
# Company packets and replies
# --------------------------------------------------------------------------- #
cpacket = classify.build_company_packet(
    name="Condor", url="https://condor.com/about", page_text="We raised a Series B.")
check("the company packet is fenced",
      cpacket.startswith("<company_page>") and cpacket.endswith("</company_page>"))
check("the packet names the URL it was fetched from",
      "https://condor.com/about" in cpacket,
      "so the model can notice it has been handed the wrong company and decline")
check("the packet names the company on the record", "Condor" in cpacket)
check("an empty page is labelled rather than silently blank",
      "(empty)" in classify.build_company_packet(name="X", url="u", page_text=""))

stage, band, note, ok = classify.parse_company_reply(
    "FUNDING: Series B\nEMPLOYEES: 51-200\nREASON: about page says both.")
check("a clean company reply parses",
      (stage, band, ok) == ("Series B", "51-200", True))

stage, band, note, ok = classify.parse_company_reply(
    "FUNDING: Series B\nEMPLOYEES: NONE\nREASON: no headcount stated.")
check("half an answer is kept, unlike the posting parser",
      (stage, band, ok) == ("Series B", None, True),
      "a page very often states one and not the other, and refusing both "
      "would throw away the fact that was found")

stage, band, note, ok = classify.parse_company_reply(
    "FUNDING: NONE\nEMPLOYEES: NONE\nREASON: the page says neither.")
check("declining both is a completed lookup, not a failure",
      ok is True and note is not None,
      "'this website does not say' and 'try again' must stay distinguishable")

check("a reply where neither field parses writes nothing",
      classify.parse_company_reply("I think they're Series B.")[3] is False)
check("an invented stage is refused",
      classify.parse_company_reply(
          "FUNDING: Series B-ish\nEMPLOYEES: about 60")[3] is False)
check("a headcount that is not a band is refused",
      classify.parse_company_reply("FUNDING: NONE\nEMPLOYEES: 60")[3] is False,
      "'60' is a number, not one of the published bands")
check("a valid band alongside a bad stage keeps the band",
      classify.parse_company_reply(
          "FUNDING: enormous\nEMPLOYEES: 1000+\nREASON: x")[:2] == (None, "1000+"))

# --------------------------------------------------------------------------- #
# The vocabularies are the contract
# --------------------------------------------------------------------------- #
check("no picklist value is empty or duplicated",
      all(len(set(v)) == len(v) and all(x.strip() for x in v)
          for v in (classify.SENIORITY_VALUES, classify.SPECIALITY_VALUES,
                    classify.FUNDING_STAGES, classify.EMPLOYEE_BANDS)))
check("no value collides with the decline token",
      classify.DECLINE not in (classify.SENIORITY_VALUES + classify.SPECIALITY_VALUES
                               + classify.FUNDING_STAGES + classify.EMPLOYEE_BANDS))

print("\n{} failed".format(len(failures)) if failures else "\nall passed")
sys.exit(1 if failures else 0)
