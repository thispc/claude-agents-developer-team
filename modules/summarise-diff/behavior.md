# summarise-diff

**Not hashed.** Reword freely.

Given two texts, report how many lines were added, removed, and left alone.

## Splitting text into lines

Pinned, because two implementations that disagree here disagree about everything
downstream:

- `""` is **zero** lines. Not one empty line.
- A single trailing newline is dropped: `"a\n"` is one line, `["a"]`.
- A second trailing newline is not: `"a\n\n"` is two lines, `["a", ""]`.
- `\r` is an ordinary character. `"a\r\nb"` is `["a\r", "b"]`. Normalising it
  would be a helpful guess, and a helpful guess is exactly the kind of thing two
  implementations make differently.

## Counting

`unchanged` is the length of the **longest common subsequence** of the two line
sequences. Then `removed = before.length - unchanged` and
`added = after.length - unchanged`.

The subtle reason this is specifiable at all: an LCS is not unique, but its
*length* is. Two implementations may pick different common subsequences and must
still return the same number. That is what makes this a fair test of equivalence
rather than a test of whether both authors copied the same algorithm.

A line that was edited therefore counts as one removed and one added. There is no
notion of "modified", which keeps the output honest about what is actually known.

`summary` is `"no change"` when nothing moved, and `"+{added} -{removed}"`
otherwise. Exact, including the spacing.

## Refusing

- `EBADINPUT` — either side is missing or is not a string. No coercion: a caller
  that passes a number has a bug, and silently stringifying it hides the bug.
- `ETOOBIG` — either side exceeds 5,000 lines. The comparison is quadratic, so
  without a ceiling a large input is a hang rather than an answer, and a hang is
  the worst failure a module can have because nothing downstream can distinguish
  it from slow.

## Shared state

None. Everything arrives in the input.
