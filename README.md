# onbgm

English · [简体中文](https://github.com/anelikes/onbgm/blob/main/README.zh-CN.md) · Website: [onbgm.com](https://onbgm.com) 

**Instrumental background music, written note by note by AI agents.**
An agent writes a plain-text score (YAML): sections, chords, melodies, drum patterns. onbgm arranges, performs, mixes and renders it into a finished track.

Unlike prompt-to-audio models, the music is text you can read and diff:
- **Exact timing.** Tempo, section boundaries, "the climax lands at 0:42" and loop points are exact, not approximate.
- **Local edits.** Say "bring the climax in 4 bars earlier" in a chat; the agent edits those 4 bars, and everything else stays identical, note for note.
- **Reproducible.** The same score always renders to the same audio.
- **No vocals, by design.** onbgm is for BGM: videos, games, podcasts, apps.

## Listen

Each track below was composed end to end by a Claude agent using onbgm:
- the agent read the docs, wrote the score, rendered drafts, read the loudness and balance reports, and revised;
- a second agent reviewed the score before it was accepted.

Click a piano roll to play the MP3, or open the score that produced it.

| Piano roll (click to listen) | Track |
|---|---|
| <a href="https://github.com/anelikes/onbgm/blob/main/docs/samples/windows-down-whistle/windows-down-whistle.mp3"><img src="https://github.com/anelikes/onbgm/raw/main/docs/samples/windows-down-whistle/pianoroll.png" width="420"></a> | **Windows Down Whistle** · travel vlog · 2:03<br>Whistled road-trip folk: steel-guitar strums, a skipping whistle hook, three rises and falls up to a final sing-along chorus.<br>[▶ Listen](https://github.com/anelikes/onbgm/blob/main/docs/samples/windows-down-whistle/windows-down-whistle.mp3) · [Score](https://github.com/anelikes/onbgm/blob/main/docs/samples/windows-down-whistle/score.yaml) |
| <a href="https://github.com/anelikes/onbgm/blob/main/docs/samples/candy-gloss/candy-gloss.mp3"><img src="https://github.com/anelikes/onbgm/raw/main/docs/samples/candy-gloss/pianoroll.png" width="420"></a> | **Candy Gloss** · product launch · 1:30<br>Bubbly future house in Eb: a supersaw hook over a canon-style descending bass, with a kick-less bell interlude before the last chorus.<br>[▶ Listen](https://github.com/anelikes/onbgm/blob/main/docs/samples/candy-gloss/candy-gloss.mp3) · [Score](https://github.com/anelikes/onbgm/blob/main/docs/samples/candy-gloss/score.yaml) |
| <a href="https://github.com/anelikes/onbgm/blob/main/docs/samples/island-hopper/island-hopper.mp3"><img src="https://github.com/anelikes/onbgm/raw/main/docs/samples/island-hopper/pianoroll.png" width="420"></a> | **Island Hopper** · game, seamless loop · 0:59<br>Sun-drenched calypso: syncopated marimba, a whistle answering each phrase, a 3+3+2 bass, and a lift into bright secondary dominants.<br>[▶ Listen](https://github.com/anelikes/onbgm/blob/main/docs/samples/island-hopper/island-hopper.mp3) · [Score](https://github.com/anelikes/onbgm/blob/main/docs/samples/island-hopper/score.yaml) |
| <a href="https://github.com/anelikes/onbgm/blob/main/docs/samples/first-steps-on-the-wide-land/first-steps-on-the-wide-land.mp3"><img src="https://github.com/anelikes/onbgm/raw/main/docs/samples/first-steps-on-the-wide-land/pianoroll.png" width="420"></a> | **First Steps on the Wide Land** · game world map, seamless loop · 2:02<br>Orchestral adventure: a marching stride, a bugle-like violin theme with cello counterpoint, a B-minor shadow, then a full tutti.<br>[▶ Listen](https://github.com/anelikes/onbgm/blob/main/docs/samples/first-steps-on-the-wide-land/first-steps-on-the-wide-land.mp3) · [Score](https://github.com/anelikes/onbgm/blob/main/docs/samples/first-steps-on-the-wide-land/score.yaml) |
| <a href="https://github.com/anelikes/onbgm/blob/main/docs/samples/pressure-cooker/pressure-cooker.mp3"><img src="https://github.com/anelikes/onbgm/raw/main/docs/samples/pressure-cooker/pianoroll.png" width="420"></a> | **Pressure Cooker** · film, emotional conflict · 1:59<br>A G-minor slow burn: spiccato basses and grinding violins build pressure bar by bar until a full-ensemble eruption, then a lone pedal tone.<br>[▶ Listen](https://github.com/anelikes/onbgm/blob/main/docs/samples/pressure-cooker/pressure-cooker.mp3) · [Score](https://github.com/anelikes/onbgm/blob/main/docs/samples/pressure-cooker/score.yaml) |
| <a href="https://github.com/anelikes/onbgm/blob/main/docs/samples/night-before-deadline/night-before-deadline.mp3"><img src="https://github.com/anelikes/onbgm/raw/main/docs/samples/night-before-deadline/pianoroll.png" width="420"></a> | **Night Before Deadline** · lo-fi study, seamless loop · 2:29<br>Hard-hitting boom-bap: a driving bass riff, a bouncing piano motif over Rhodes stabs, and a half-time section before the octave-up peak.<br>[▶ Listen](https://github.com/anelikes/onbgm/blob/main/docs/samples/night-before-deadline/night-before-deadline.mp3) · [Score](https://github.com/anelikes/onbgm/blob/main/docs/samples/night-before-deadline/score.yaml) |

The comments at the top of each score are the agent's own composition notes, written in Chinese.

## How it works

```
agent ──writes / edits──▶ score YAML ──▶ strict validation + timeline solving ──▶ textures + humanization ──▶ notes
              ▲                                                                                              │
              │                                stems (built-in sampler · SFZ libraries · synth · SF2 via FluidSynth)
              │                                                                                              │
              └── report: loudness vs. energy, part balance, warnings, piano roll ◀── mix (role levels, seating, hall) → master
```

The agent describes *what* the music should do. The engine handles *how* it is performed: velocity, pedaling, expression, micro-timing, voice leading, mixing and mastering.

## A score at a glance

```yaml
meta: {bpm: 84, key: "A minor", loop: true}
sections:
  - {name: intro, bars: 4, energy: 0.3}
  - {name: theme, bars: 8, energy: "0.5->0.8"}
harmony:
  intro: "Am | F | C | G"
  theme: "Am | F | C | G | Dm | F | Esus4 | E"
tracks:
  keys:  {sound: epiano, texture: comp, rhythm: "x--- ---- x-x- ----"}
  bass:  {sound: electric-bass, texture: bassline, rhythm: "1--- ---- 1-5- ----"}
  drums:
    sound: drums-jazz
    texture: groove
    in: [theme]
    pattern: {kick: "x... ...x ..x. ....", snare: ".... x..g .... x...", hat: "x.x. x.x. x.x. x.x."}
  lead:
    sound: vibraphone
    texture: melody
    in: [theme]
    notes:
      theme: "E5/4. D5/8 C5/4 A4/4 | C5/2 r/2 | G5/4. E5/8 D5/4 C5/4 | D5/2 r/2 |
              F5/4 E5/4 D5/4 C5/4 | A4/2 C5/4 D5/4 | B4/2 G#4/2 | A4/1"
```

Chords are written per bar. Accompaniment uses *textures*, such as arpeggio, pad, comp, strum, bassline and groove. Melodies are written note by note. The engine also supports:
- per-section tempo changes and ritardandos;
- 3/4, 6/8 and 12/8 meters;
- swing and lo-fi coloring;
- seamless loops;
- a sound-effect timeline (risers, impacts, whooshes) that stays in sync with the music.

The full format is in [`skills/onbgm/references/score-format.md`](https://github.com/anelikes/onbgm/blob/main/skills/onbgm/references/score-format.md), and the JSON Schema is in [`schema/score.schema.json`](https://github.com/anelikes/onbgm/blob/main/schema/score.schema.json).

## Quick start

```bash
brew install fluid-synth                  # Debian/Ubuntu: sudo apt install fluidsynth
uv tool install onbgm                     # or: pip install onbgm
onbgm fetch                               # download sound libraries to ~/.onbgm/sounds (~1.7 GB; set ONBGM_SOUNDS to change)
onbgm doctor                              # check the environment

onbgm init my_bgm --example late_study    # start from an example
onbgm catalog -q "lazy lo-fi drums"       # search ready-made patterns
onbgm check my_bgm/score.yaml             # validate and print the timeline
onbgm fit 0:07.3=reveal 0:21.8=feature --length 0:45 --write video.yaml   # scoring a video: tempo + structure from cut points
onbgm render my_bgm/score.yaml -o my_bgm/out/v1               # render
onbgm render my_bgm/score.yaml -o my_bgm/out/v1 --soundset gm # GM sounds only (fast, light)
onbgm review my_bgm/out/v1 --compare my_bgm/out/v0            # listen in the browser, A/B, leave comments
```

Every command supports `--json`. Errors come with a location, a reason and a fix, so an agent can act on them directly.

Output files:
- `loop.wav`: a sample-accurate seamless loop. Non-looping tracks get `song.wav`, with the exact score length.
- `preview.mp3`, `song.mid`, `stems/`, `pianoroll.png`.
- `player.html`: a self-contained player with a piano roll, sections and chords.

## For agents

The agent-facing docs are in [`skills/onbgm/`](https://github.com/anelikes/onbgm/blob/main/skills/onbgm/SKILL.md), organized as a skill:
- [`SKILL.md`](https://github.com/anelikes/onbgm/blob/main/skills/onbgm/SKILL.md): the workflow, how to read the render report, and the rules.
- [`references/cli.md`](https://github.com/anelikes/onbgm/blob/main/skills/onbgm/references/cli.md): commands, `--json` output and error codes.
- [`references/score-format.md`](https://github.com/anelikes/onbgm/blob/main/skills/onbgm/references/score-format.md): the complete score format.
- [`references/arranging.md`](https://github.com/anelikes/onbgm/blob/main/skills/onbgm/references/arranging.md): how to write good BGM, with recipes per style.

The docs are currently in Chinese. Agents read them without trouble; an English translation is planned.

## Reviewing in the browser

`onbgm review out/v2 --compare out/v1` opens a local page where you can:
- see the piano roll, sections, chords and sound effects, and click anywhere to play from there;
- switch seamlessly between two versions at the same position;
- leave comments on the timeline.

Comments are saved to `review.json`. Each one includes the time, bar, beat, section and chord, so the agent can act on it directly.

## Examples

| Example | Style |
|---|---|
| `night_rain_v1` / `v2` | Cinematic piano and strings. v2 shows conversational editing: climax 4 bars earlier, bass switched to pizzicato |
| `late_study` | Lo-fi hip hop: jazz chords, swing, vinyl texture |
| `sunny_walk` | Acoustic guitar vlog: strumming, claps, glockenspiel |
| `cafe_jazz` | Café jazz piano trio |
| `vlog_intro` | A 20-second non-looping intro |
| `product_video` | A 45-second product video: synths only, synced to cut points, with a sound-effect timeline |

`cafe_jazz` and `vlog_intro` were written by fresh agents that had only read the docs. `experiments/` keeps the raw records of those usability tests.

## Sounds

| Source | License | Used for |
|---|---|---|
| [GeneralUser GS](https://github.com/mrbumpy409/GeneralUser-GS) | Free use | GM fallback for every sound (`--soundset gm`) |
| [Salamander Grand Piano](https://freepats.zenvoid.org/Piano/acoustic-grand-piano.html) | CC-BY 3.0 | Piano |
| [VSCO 2 Community Edition](https://github.com/sgossner/VSCO-2-CE) | CC0 | String sections (incl. spiccato), contrabass, pizzicato, timpani, cymbals, glockenspiel |
| [jRhodes (GM)](https://github.com/sfzinstruments/Discord-SFZ-GM-Bank), Jeff Learman | CC0 | Electric piano |
| [FSS Steel String Guitar](https://freepats.zenvoid.org/Guitar/steel-acoustic-guitar.html), Gary Campion / FreePats | GPL-3.0+ with an exception: music made with it is not covered by the GPL | Acoustic guitar |
| [Black And Blue Basses](https://github.com/sfzinstruments/karoryfer.black-and-blue-basses), Karoryfer Samples | CC0 | Electric bass |
| [AVL Drumkits](https://www.bandshed.net/avldrumkits/) (Black Pearl, Blonde Bop), Glen MacArthur | CC-BY-SA 3.0; music made with them needs no attribution | Standard and jazz drum kits |
| [Swirly Drums](https://github.com/sfzinstruments/karoryfer.swirly-drums), Karoryfer Samples | CC0 | Brush kit (dry mics only) |
| [Versilian Community Sample Library](https://github.com/sgossner/VCSL), Versilian Studios | CC0 | Vibraphone, marimba, harp, tenor saxophone, dan tranh zither |
| [AliExpress Erhu](https://github.com/sfzinstruments/aliexpress-erhu), sfzinstruments | CC0 | Erhu |
| [Emilyguitar](https://github.com/sfzinstruments/karoryfer.emilyguitar), Karoryfer Samples | CC0 | Electric guitar (clean and distorted) |
| [FreePats Ukulele](https://github.com/freepats/ukulele1), [Button Accordion HN](https://github.com/freepats/button-accordion-HN) | CC0 | Ukulele, accordion |
| Built-in synth (`onbgm/synth.py`) | — | Synth plucks, pads, supersaw, leads, basses, 808, FM bell and e-piano, synth drums, and 8-bit chip sounds (NES-style pulse, triangle and noise drums); no download needed |

Samples are played by the built-in sampler (`onbgm/sampler.py`), which reads SFZ directly and uses soxr for high-quality pitch shifting. On top of what the libraries define, it calibrates a few things:
- **Onsets.** It compensates sample pre-roll so attacks land on the beat.
- **Dynamics.** It level-matches velocity layers and round robins.
- **Tuning.** It retunes hand-played samples.
- **Extras.** It can add vibrato or effect chains, such as the distorted guitar's amp and cab.

All downloads are pinned to a specific version, so library updates never change how an existing score sounds. If an `hq` library is missing, that instrument falls back to GM.

## Design principles

- **The agent describes intent; the engine performs.** Velocity, pedaling, expression and timing nuance come from texture code, not from the agent.
- **Local edits stay local.**
  - Humanization is keyed by part, section, position and pitch.
  - Voice leading restarts every section.
  - Accompaniment levels come from each part's configuration, not its content.
  - Mastering uses a fixed gain.

  So editing one section leaves every other section bit-identical.
- **The agent doesn't have to mix.** Parts are leveled by role, the melody is balanced against each section's accompaniment, and a look-ahead limiter catches peaks.
- **Errors are written for agents.** Every error states its location, the reason and the fix. Typos get a "did you mean". Unknown fields are always errors, never silently ignored.

## FAQ

**On Linux, onbgm crashes with `Illegal instruction`, or reports `pedalboard_illegal_instruction`.**
This is a known issue in the audio library pedalboard ([spotify/pedalboard#454](https://github.com/spotify/pedalboard/issues/454)). Its Linux x86 wheels up to 0.9.25 are compiled for the build machine's CPU, so they crash on some CPUs, such as certain AMD EPYC servers. Upstream has fixed it, and the fix is waiting for a release. Until then, onbgm detects the problem at startup and prints the commands to build the fixed version (about 4 minutes).

## Development

```bash
uv venv && uv pip install -e ".[dev]"   # in a source checkout, sounds download to ./sounds
.venv/bin/python -m pytest              # audio tests skip themselves without fluidsynth or sounds
```

See [CONTRIBUTING.md](https://github.com/anelikes/onbgm/blob/main/CONTRIBUTING.md) for conventions and for adding instruments or sample libraries, and [ROADMAP.md](https://github.com/anelikes/onbgm/blob/main/ROADMAP.md) for scope and plans.

## License

The code is [MIT](https://github.com/anelikes/onbgm/blob/main/LICENSE)-licensed.

The sound libraries are not part of this repository. `onbgm fetch` downloads them from their authors, and their licenses are listed in the table above. For music made with them:
- the CC0 libraries (VSCO, VCSL, jRhodes, the Karoryfer libraries, FreePats ukulele and accordion, the erhu) have no requirements;
- the authors of GeneralUser GS, the FSS guitar and the AVL kits state explicitly that music made with them is free to use, including commercially;
- Salamander Grand Piano is CC-BY 3.0, and its author says nothing specific about music made with it. To be safe, credit "Piano: Salamander Grand Piano by Alexander Holm (CC-BY 3.0)" when publishing work that uses the piano.

## Contact

[contact@onbgm.com](mailto:contact@onbgm.com)
