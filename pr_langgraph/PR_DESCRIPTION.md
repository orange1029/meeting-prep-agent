# Raise a useful error when a conditional router returns a non-`path_map` value

Fixes #9319

## Status

Auto-closed by the repo's `require_issue_link` gate: external contributors must
be assigned to the linked issue. Requested assignment in
[#9319](https://github.com/langchain-ai/langgraph/issues/9319#issuecomment-6183912806);
the bot reopens the PR automatically once that happens.

As of 2026-10-19 a maintainer has confirmed this is the tracked place for
exactly this change — see [Status in upstream](#status-in-upstream) below.

Title follows `pr_lint.yml` (`fix(langgraph): ...`, semantic type + scope).

## Status in upstream

On 2026-10-19 maintainer @eliornl closed #6877 with:

> thanks! closing in favor of #6771, which makes the same fix for routing to
> `END` without a `path_map` entry. **the clearer error for unknown keys is
> tracked in #9319.**

So upstream has split the two concerns apart:

- **routing to `END` without a `path_map` entry** → #6771 (a behaviour change)
- **a clearer error for unknown keys** → **#9319, this PR**

`main` still raises a bare `KeyError` at the `path_map` lookup
(`self.ends[r]` in `Branch._finish`); there is no `_resolve_destination`
symbol upstream. This PR is the only open implementation of the error-message
half.

## Summary

When a routing function passed to `add_conditional_edges` returns a value that
isn't a key of `path_map`, the graph fails at runtime with a bare:

```
KeyError: 'count_retry'
```

The message names neither the node whose router misbehaved, nor the values that
would have been accepted. Since `path_map` is spelled out separately from the
router, returning a node name instead of a `path_map` key is an easy mistake to
make — and the current error gives the reader nothing to act on.

This change raises a `ValueError` that names the offending value, lists the
valid keys, and says which of the two sides to fix. The neighbouring checks in
`_finish` already raise `ValueError`/`InvalidUpdateError` with that kind of
context; the `path_map` lookup was the one that didn't.

## Details

In `langgraph/graph/_branch.py`, `Branch._finish` resolves each routing result
through `self.ends[r]`. Adding `_resolve_destination` alongside it keeps the
lookup in one place and covers both `path_map` forms (`dict` and `list`) plus
the `None` case, since `self.ends` is only consulted when it's non-empty.

```python
def _resolve_destination(self, route_key: Hashable) -> str:
    try:
        return self.ends[route_key]
    except KeyError:
        valid = sorted(map(repr, self.ends))
        raise ValueError(
            f"Conditional edge returned {route_key!r}, which is not a key in "
            f"path_map. Valid keys: {valid}. Either return one of these from "
            f"the routing function, or add {route_key!r} to path_map."
        ) from None
```

Resulting message:

```
ValueError: Conditional edge returned 'count_retry', which is not a key in
path_map. Valid keys: ["'b'", "'c'"]. Either return one of these from the
routing function, or add 'count_retry' to path_map.
```

## Scope

- Only `_finish`'s lookup changes; routing behaviour is untouched.
- `Send` objects still bypass the lookup.
- With no `path_map`, `self.ends` is empty and the lookup never runs, so that
  path is unchanged.
- `from None` suppresses the `KeyError` context, which would otherwise be
  prepended to every message and reintroduce the noise.

## Tests

`tests/test_branch_path_map_error.py` — 8 cases:

- the error names the bad value, the valid keys, and `path_map`
- the exception is a `ValueError`, not a `KeyError` (regression guard)
- routing still works for: single destination, multiple destinations,
  `path_map` as a list, no `path_map`, `Send` passthrough, non-`str` keys

Verified the two error tests fail against the unpatched `_branch.py`
(`KeyError: 'count_retry'`) while the six routing tests pass in both states, so
the behavioural cases assert compatibility independently of the fix.

## Note on related reports

Related work, and how it differs:

- **#6771** (open, the one @eliornl kept) — treats `END`/`__end__` as an
  implicit pass-through when it's absent from `path_map`. That is a
  *behavioural* change: it changes which values are accepted. This PR does
  not touch that; it only changes the error message when a value is rejected,
  so the two compose.
- **#6877** (closed) — the only earlier attempt at both. It also introduced a
  `_resolve_destination` helper, but its message doesn't list the valid keys
  and doesn't say which side to fix.
- **#6770** (issue, still open) and the duplicate PRs **#6888** / **#6926** /
  **#7064** — all about `END`/`__end__` not being present in `path_map`.
