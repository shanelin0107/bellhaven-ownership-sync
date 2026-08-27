# Writeup

## My matching approach

I stopped trusting names about ten minutes in. Two of the facilities on the
website had been rebranded past recognition, so Sunny Acres Retirement Home is
now Bellhaven Willow Creek and Chesterton Senior Commons is now Bellhaven of
Chesterton. Nothing in either pair overlaps. Then I found the opposite problem:
the website lists an Amberly Manor in Hudson, Ohio, and the CRM has an Amberly
Manor in Colorado Springs owned by Juniper Point. Same name, different company,
two thousand kilometres apart. A name match would have pulled a competitor's
account under Bellhaven and missed the Ohio facility that actually needed
creating. Two errors from one shortcut.

So I match on address. Street strings get normalized first, because the same
building shows up as Wilmington Pike and Wilmington Pk, as W Lake Rd and West
Lake Road. Then four tiers, from exact street plus zip down to a name similarity
fallback for the one account whose billing address is a PO box. Every tier is
gated on state and city, and name similarity is never allowed to decide on its
own. That gate is the only thing keeping Colorado out of Ohio.

Duplicates needed a second layer. Five buildings had more than one account, and
picking a survivor by feel is not reproducible, so I wrote an ordered ladder:
billing history first, then whether the account holds the administrator the
website names, then phone, then name, then parent, then completeness. The
website turned out to be the strongest evidence available. Three of the five
groups were settled by the administrator's name, including one where both copies
were called Bellhaven of Owosso and sat under the same parent. Nothing inside the
CRM could separate them.

When nothing separates a group at all, which happened once at Kettering, I take
the lowest account id and say so in the note. It is arbitrary, but it is the same
arbitrary answer every run, and a daily job that picks a different survivor each
night is worse than one that picks a defensible one and documents it.

## How I used AI tools

I built this with Claude Code, and the rule I set for myself was that nothing
goes into the CRM or the writeup unless I have seen it checked against the real
data. Generating code was the fast part. Verifying it was where the time went,
and where the value was.

That discipline paid for itself several times. My address normalizer silently
turned "750 Stewart Road" into "750 rd" because a regex meant to strip suite
numbers was matching the "Ste" inside Stewart. The duplicate survivor logic
picked a Kettering account on a name similarity gap of 0.009 and reported it as
high confidence, which is string comparison noise dressed up as evidence. The
review app was writing dry run approvals into the real decision ledger, which
would have marked those proposals as decided forever and made them impossible to
approve for real later. None of those show up unless you look at the output.

There was also a mistake I made while probing the write API. The OpenAPI spec
declares no request body for POST or PATCH, so I sent deliberately invalid
requests to learn what the server enforces. I assumed pointing
`duplicate_of_account` at its own account would be rejected. It was not. I wrote
a self-referencing loop into a live record and had to revert it. That is now one
of three checks in the client, because it turns out the server validates
`parent_id` and nothing else.

The judgement calls were mine. Whether an account missing from a marketing site
is enough reason to deactivate it, whether Union Square is one facility or two,
whether Kettering deserves a Needs Review flag or just an honest note. Those are
sales operations questions, not engineering ones, and the reasoning behind each
one is in the README and in the note field on the records themselves.

## What I would build next

The review app lets you approve half of a duplicate group. If someone approves
the two dedupe proposals for Kettering and rejects the re-parent on the survivor,
the CRM ends up with two inactive accounts pointing at a record that still has
the wrong parent and the old name. Proposals that belong to one decision should
be linked so they move together.

Contacts on a losing duplicate stay where they are. The Owosso loser has an
admissions director with an email address the survivor does not have, and that
contact is now sitting on a deactivated record. Moving contacts to the survivor
is the obvious next step and the API supports it.

The bigger gap is that the website is my only source. An operator updates their
marketing site when they get around to it, but ownership changes appear in state
licensing records and CMS provider data first. A second source would catch a
change weeks earlier and would give me a way to confirm the cases where the
website alone is not enough, which is exactly the situation Sandusky is in right
now.
