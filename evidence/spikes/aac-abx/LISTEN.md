# Spike 0.4 — Blinded stem-codec listening test

**What this decides:** which per-stem delivery codec (AAC 256k, AAC 320k, or
lossless ALAC) the pipeline ships. The stems were encoded per codec, decoded,
and summed at unity — exactly what the player does — so any artifact you hear
is stem-codec damage, not mixing or remix-encoding damage (the remix files
themselves are all lossless ALAC).

**Song:** the shortest k25 library track (job `47bae048e13c`, 3:20), separated
with htdemucs_6s, float stems, one common gain.

## Files (`out/`)

| File | What it is |
|---|---|
| `REF.m4a` | Reference: unity remix of the raw float stems (lossless) |
| `A.m4a` `B.m4a` `C.m4a` | The three codec renditions, randomized order |

Do **not** open `out/answer_key.json` or the `out/private/` folder until you
have written down your verdicts — that's the unblinding.

## How to listen

1. Play on the real rig (the karaoke output chain, not laptop speakers).
2. Level-match is already handled — all four files went through the same gain.
3. For each of A, B, C: ABX against `REF.m4a` — flip between the two at the
   same spot in the song. Good spots: dense choruses (codec stress),
   cymbal/hi-hat decay, vocal sibilance, quiet outro.
4. Write down, before unblinding:
   - For each of A/B/C: "distinguishable from REF: yes / no / unsure"
   - A ranking of A/B/C by preference, ties allowed.
5. Then open `out/answer_key.json` and record the verdict in
   `../RESULTS-0.3-0.4.md` (replace the PENDING line).

**Decision rule (suggested):** if you cannot reliably distinguish AAC 256k
from REF on the real rig, ship 256k; else if 320k is indistinguishable, ship
320k; else ALAC.
