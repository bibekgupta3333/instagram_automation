# 26 — Nursery-song channel (3D sing-along, UK/IE/EU and AU/NZ English)

> **Status:** proposed pilot · **Researched:** 2026-10-23 · Supersedes nothing; extends [19](19-kids-channel-opportunity.md), [20](../2-channels/20-pocket-meadow-question-club.md) and [21](../4-production/21-kids-pilot-production.md). Figures are dated and sourced; anything marked *est.* or *unverified* is not a measured fact. This is research, not legal advice: get a UK/EU/AU media lawyer to sign off the rights and compliance sections before launch.

**User direction (2026-10-23):** a CoComelon-style channel only (3D-animated nursery songs for babies and toddlers), in English for the UK, Ireland, the EU and Australia/NZ, built for maximum reach (the user mentions ~1 billion views), with every character, song and arrangement original.

**Bottom line.**
- The genre is the largest audience on YouTube, and the winning recipe is well known: simple repeated songs, everyday routines, a warm family cast, bright 3D, and long compilations that pile up watch time.
- The same recipe is now YouTube's main enforcement target for AI-made kids content. After a NYT investigation in February 2026, YouTube removed AI toddler channels from the Partner Program.
- Made-for-kids (MFK) videos earn contextual ads only, at low RPM.
- One billion views is franchise scale. Moonbug reports more than 4 B *monthly* views for CoComelon alone. It is not a target for a new channel.
- Our edge is to be clearly better made and original, not faster or more numerous.
- **Publishing is blocked today.** `studio/publish/gate.py` rejects `made_for_kids=True`, and this doc does not change that (§3.7).

---

## 1. Genre playbook

### 1.1 Who wins, with dated numbers

| Channel / owner | Reported figure | Date | Source |
|---|---|---|---|
| CoComelon (Moonbug) | "more than 4 billion average monthly views on YouTube" | 5 Nov 2025 | [Moonbug press release](https://www.moonbug.com/news/universal-pictures-will-release-moonbug-entertainment-and-flywheel-media-s-cocomelon-the-movie-pro) |
| CoComelon main channel | ~200 M+ subscribers, ~220 B lifetime views | Apr 2026 | [Wikipedia](https://en.wikipedia.org/wiki/Cocomelon) (secondary); Social Blade showed ~202 M / ~225 B in Jul 2026 |
| Pinkfong "Baby Shark Dance" | 16.7 B views; YouTube's #1 video for 64 consecutive months | Mar 2026 | [Khan Sports, Pinkfong announcement](https://sports.khan.co.kr/en/article/202603242205007) |
| The Pinkfong Company (all channels) | 66.5 B YouTube views in 2025 (+52% YoY); 210 B cumulative; 300 M subscribers; 7,000+ titles in 25 languages | 24 Mar 2026 | same |
| Bebefinn (Pinkfong) | 10 M subscribers, 3 B views and 210 M watch hours **in its first 14 months** (launched 3 Apr 2022; US is its top market) | Jun 2023 | [Licensing International](https://licensinginternational.org/news/pinkfongs-bebefinn-surpasses-10-million-youtube-subscribers-setting-new-company-record/) |
| Bebefinn | 23.9 B views in 2025 (+42%); 51.3 B cumulative | Mar 2026 | [Khan Sports](https://sports.khan.co.kr/en/article/202603242205007) |
| Super Simple Songs | ~46 M subscribers, ~63 B lifetime views | Aug 2026 | Tubefilter via [ATDb](https://www.theatdb.com/news/have-you-heard-20-years-of-super-simple-songs-amp-in-the-club-and-shyamalans-next-twist) (secondary) |
| Little Baby Bum (Moonbug) | ~42 M subscribers, ~44 B views | Jul–Aug 2026 | [Social Blade](https://socialblade.com/youtube/handle/littlebabybum) (third-party tracker) |
| Lalafun – Nursery Rhymes | ~10.7 M subscribers, ~6.3 B views (Sep 2025); later profile ~13 M | Sep 2025 | [vidIQ](https://vidiq.com/youtube-stats/channel/@lalafun-kids-nursery-rhymes/) (third-party estimate) |

**What this says.** Every leader above is a studio with years of catalogue, several channels and many languages. Bebefinn's 14-month rise was a new IP from the company that already owned YouTube's most-viewed video, with that studio's cross-promotion, budget and catalogue behind it. It is the best case, not a base rate.

### 1.2 What the hits have in common

These patterns come from watching the reference channels. Where a claim has research behind it, the source is cited. Copy the *principles*, never the songs, characters or look (§2).

| Element | Pattern | Evidence / note |
|---|---|---|
| **Song structure** | Verse plus a short chorus that repeats 3–6 times. Each verse swaps one variable (an animal, a body part, a family member, a step in a routine) and keeps the same melody, so the child can predict what comes next. Call-and-response lines. Counting up or down. Animal sounds and other onomatopoeia. | Standard nursery-song form. "Baby Shark" is a one-variable cumulative song. |
| **Melody and pitch** | Narrow range of about an octave, mostly stepwise, a major key, a simple I–IV–V harmony. A higher, warm vocal. | Infants prefer higher-pitched infant-directed singing ([Trainor & Zacharias 1998](https://trainorlab.mcmaster.ca/publications/pdfs/trainor_zacharias_1998.pdf)). Infant-directed versions are sung higher and slower ([Trehub et al. 1997](https://trainorlab.mcmaster.ca/publications/TrehubEtAl1997)). Play songs are more rhythmic than lullabies ([Trainor 1996](https://trainorlab.mcmaster.ca/publications/pdfs/trainor_1996.pdf)). |
| **Tempo** | *Working assumption, not a sourced figure:* play and routine songs at about 90–120 BPM, lullabies and bedtime songs at about 60–80 BPM. Measure reference tracks ourselves before fixing house tempos. | No reliable published BPM study of CoComelon was found. Blog figures disagree. |
| **Palette** | Bright, saturated primaries and pastels on clean, uncluttered sets. Soft daylight. Strong contrast between a character and its background. | Observed. Keep our own palette distinct from theirs (CoComelon green and red, Pinkfong pink). |
| **Camera and editing** | Slow dolly and orbit moves, eye-level framing, faces held long enough to read. | A randomised study found 9 minutes of a fast-paced cartoon immediately impaired 4-year-olds' executive function, compared with a slow educational show; scene changes were every 11 s vs 34 s ([Lillard & Peterson 2011, *Pediatrics*](https://pmc.ncbi.nlm.nih.gov/articles/PMC9923845)). CoComelon is widely criticised for fast cuts (secondary sources only). **We choose slower pacing:** that is both the ethical and the policy-safe choice. |
| **Cast** | A small family: a toddler lead, siblings, parents, grandparents and a pet, plus friends from diverse backgrounds. A consistent home and neighbourhood. | YouTube's high-quality principles reward "shows them the world" and "positive role models" ([YT 10774223](https://support.google.com/youtube/answer/10774223)). |
| **Topics** | Everyday routines: bath, bedtime, eating vegetables, potty and nappy change, brushing teeth, tidying up, getting dressed, saying sorry and sharing, going to the doctor, a new sibling, the first day at nursery. Plus counting, colours, animals, vehicles and the weather. | Routines are searched by parents ("bath song", "brush teeth song"). They also match the "looking after yourself" and "life skills" principles. |
| **Titles** | Plain and descriptive, e.g. "Bath Song \| Nursery Rhymes for Toddlers". Compilations say "+ More". | Keyword stuffing is classed as low quality (§3.4). |

### 1.3 Length and format mix

| Format | Length | Job |
|---|---|---|
| **Single song** | 2:00–3:30 (many hits run 2–4 min) | The new IP. It gets searched, recommended and replayed. |
| **Compilation** | 30–60 min; the leaders also run 1–3 h | Watch time and session length. Parents put it on TV or a tablet. Most views in the genre accrue to compilations of a channel's own songs (observed). |
| **Short** | 15–45 s chorus cut, vertical, self-contained | Discovery. Pinkfong credits short-form for Moonshark's +1,314% growth in 2025 ([Khan Sports](https://sports.khan.co.kr/en/article/202603242205007)). MFK Shorts lose comments; see §4 for revenue. |

**Upload cadence of the leaders (observed, unverified).**
- They post at least one new single per week per channel, plus several new compilations each week assembled from their back catalogue.
- They run many language channels at once.
- We cannot match that volume with human review, and should not try (§3.4).

**Playlist and compilation rules.**
- Group by routine and theme: "Bath & Bedtime", "Animal Songs", "Counting".
- Order playlists so autoplay flows naturally from one song to the next.
- Refresh compilations when new songs land, rather than re-cutting the same order.
- Viewers can't save MFK videos to playlists (§3.1), but channel-owned playlists still work and are the main way to steer the next view.
- A compilation of **our own** songs is not reused content, but it must be assembled deliberately. YouTube flags "generic or repetitive content … made with a template" ([YT 1311392](https://support.google.com/youtube/answer/1311392)). Give each compilation a distinct running order and theme, and one or more new songs.

---

## 2. IP rules

### 2.1 Hard exclusions

- **Moonbug / CoComelon:** no names (CoComelon, JJ, YoYo, TomTom, Cody, Nina…). No watermelon logo or motifs. No lookalike toddler with a big head and a single curl. No CoComelon intro sting, arrangements, vocal style imitation, character designs, house or school sets. The same applies to Moonbug's other brands: Little Baby Bum (Mia, Twinkle the star), Blippi, Morphle.
- **Pinkfong / Bebefinn:** no Baby Shark song in **any** version (its folk origin doesn't make Pinkfong's arrangement and recording free, and the song is too identified with Pinkfong to be worth the risk). No sharks with that dance. No pink fox. No Bebefinn family designs or name pattern (Finn, Bora, Brody).
- **Super Simple Songs, Lalafun and everyone else:** no copying of their original songs (Super Simple's "Baby Shark"-style originals, "Skidamarink" arrangements, "The Bath Song" style titles used as brand). No covers of their recordings.
- **Never** use names, characters or keywords from other channels in titles, tags or descriptions. That is IP risk, and also "keyword stuffing" under YouTube's quality principles (§3.4).
- **Never** clone a real singer's voice. No AI voice model trained on a known children's performer.

### 2.2 Public-domain rhymes: UK / IE / EU / AU / NZ

Copyright terms:
- **UK and EU (including Ireland):** author's life plus 70 years, running to 31 December ([EU Directive 2006/116/EC](https://eur-lex.europa.eu/eli/dir/2006/116/oj); UK CDPA 1988 s.12).
- **Australia:** life + 70 for authors who died in 1955 or later; works of authors who died before 1955 have expired.
- **New Zealand:** moved to life + 70 on 30 Dec 2022, not retroactively. *Verify NZ cases individually.*

PD status covers the **traditional words and tune only**. Every modern arrangement, recording, added verse and animation is a new copyright work owned by someone. Our own arrangement and recording is new work too.

| Rhyme | Status (UK/EU/AU) | Basis and caveat |
|---|---|---|
| Twinkle, Twinkle, Little Star | PD | Words by Jane Taylor (1806, d. 1824); tune "Ah! vous dirai-je, maman" (1761). |
| Alphabet song ("ABC") | PD | 1835, same tune. **UK/AU "zed" breaks the US rhyme**, so write a UK ending. |
| Baa, Baa, Black Sheep | PD | c. 1744 *Tommy Thumb's Pretty Song Book*. |
| Row, Row, Row Your Boat | PD | 1852 words; 1881 tune. The modern "crocodile … scream" verse is of uncertain origin, so don't use it. |
| Old MacDonald Had a Farm | PD | Folk; printed 1917. |
| Itsy Bitsy / **Incy Wincy** Spider | PD | 1910 print. Use the UK title "Incy Wincy". |
| Hickory Dickory Dock; Humpty Dumpty; Hey Diddle Diddle; Jack and Jill; Little Bo-Peep; Rock-a-bye Baby; Pat-a-cake | PD | 18th-century collections. |
| Mary Had a Little Lamb | PD | Sarah Josepha Hale (1830, d. 1879); tune by Lowell Mason (d. 1872). |
| London Bridge; Oranges and Lemons; The Grand Old Duke of York; Polly Put the Kettle On; Here We Go Round the Mulberry Bush; Hot Cross Buns; One, Two, Buckle My Shoe; Ring a Ring o' Roses | PD | 17th–19th-century traditional. |
| Head, Shoulders, Knees and Toes | PD (traditional) | Tune variants differ; use the traditional one. |
| Frère Jacques / Are You Sleeping | PD | 18th-century French. |
| Hush, Little Baby; Bingo | PD | US and UK folk, 18th–19th century. |
| **Happy Birthday to You** | **PD in the UK and EU since 1 Jan 2017**; PD in AU long before | Mildred Hill d. 1916, Patty Hill d. 1946, so life + 70 ran to 31 Dec 2016. In the US, the 2015 *Rupa Marya v. Warner/Chappell* ruling ended the claim and it was settled in 2016. ([overview](https://en.wikipedia.org/wiki/Happy_Birthday_to_You), secondary). Still low priority. |
| **The Wheels on the Bus** | **Do not use the lyrics** | Written by Verna Hills (1898–1990), first printed in *American Childhood* in 1937/1939 ([Wikipedia](https://en.wikipedia.org/wiki/The_Wheels_on_the_Bus); [Classic FM](https://www.classicfm.com/discover-music/wheels-on-the-bus-lyrics-composer-history/)). If she is the author, life + 70 keeps the words protected in the UK/EU/AU **until 31 Dec 2060**. The tune is the PD "Mulberry Bush", but don't build a "wheels go round and round" song on it. |
| **The Hokey Cokey** | **Avoid** | The UK version is commonly credited to Jimmy Kennedy (1942, d. 1984), which would put it in copyright until 2054 (*not verified from a primary source*). |
| Five Little Ducks; Five Little Monkeys; Five Little Speckled Frogs; Miss Polly Had a Dolly; Ten in the Bed; Wind the Bobbin Up; Sleeping Bunnies; If You're Happy and You Know It; Johny Johny Yes Papa | **Uncertain: clear before use** | 20th-century or disputed authorship. Some have a credited modern author or arrangement. Johny Johny is also a saturated, low-quality-associated keyword. |
| Baby Shark | **Excluded** | See §2.1. |

### 2.3 Recommended song mix

1. **About 70% original songs**, with original melody, lyrics, arrangement and recording. These are the only songs that build exclusive IP, and the only ones a competitor can't also upload. Routines (bath, teeth, nappy, bedtime) are open topics, but **titles must not copy a competitor's signature song title as branding**.
2. **About 30% PD rhymes, re-imagined:** the traditional verse plus **new original verses** set in our world, a new arrangement, and a new recording. Record the PD basis for each rhyme in the asset log ([21 §Rights](../4-production/21-kids-pilot-production.md)).
3. Keep a per-song rights file covering the composition, the lyrics, the master recording, the performers' consent and contracts, any AI tools with their commercial terms, and the stems. A composition and a sound recording are separate works ([21](../4-production/21-kids-pilot-production.md)).
4. **AI music tools:** use only tools whose terms grant commercial rights, and keep the human-written melody and lyrics as the source. Purely generated output may not be protectable (see the Copyright Office findings in 21). Several AI music services are in litigation with record labels, so *check each tool's status at the time of use.*

### 2.4 Language: UK/Commonwealth English

- **Spelling:** colour, favourite, mum, pyjamas, nappy, tidy, centre.
- **Vocabulary:** mum and mummy (not mom), nappy (not diaper), dummy (not pacifier), pram or pushchair (not stroller), jumper (not sweater), trousers (not pants), biscuit (not cookie), nursery (not daycare), bin (not trash can), wellies, garden (not yard), zed (not zee), queue, lorry or truck.
- **Pronunciation:** use a warm, neutral British or Commonwealth voice. For AU/NZ, an occasional episode can use local settings (beach, kindy, sunnies and hat for sun safety), but don't do caricature accents.
- **Avoid:** "thongs" (AU) and "pants" (UK), which mean different things in each market. Words AU and UK both understand travel best.
- **Reach:** the US is the largest market (Bebefinn's top country). A UK-English channel still reaches US viewers, but titles and search terms in UK vocabulary (nappy, mummy) mostly match UK, IE, AU and NZ queries. That is the user's chosen market, so accept the trade-off.

---

## 3. Compliance

### 3.1 YouTube "made for kids"

- **Classify accurately.** Content made for babies and toddlers, with nursery rhymes, cartoon characters and child-directed songs, **is made for kids**. Subject matter, characters, songs and the intended audience decide this, and a parent-facing description doesn't change it ([YT 9528076](https://support.google.com/youtube/answer/9528076)). Set it at channel level **and** on every video.
- **Ads:** contextual only. Personalised ads, remarketing and other personalised targeting are prohibited. Many ad categories are barred, including food and beverage ([YT 9713557](https://support.google.com/youtube/answer/9713557)).
- **Disabled on MFK** ([YT 9632097](https://support.google.com/youtube/answer/9632097)):
  - Viewer side: comments, notifications, cards and end screens, autoplay on home, the miniplayer, and saving to playlists or Watch Later.
  - Monetisation and interaction: channel memberships, Super Chat and Super Stickers, merchandise and ticketing shelves, the donate button, live chat, and posts.
  - Plan discovery around search, recommendations, channel playlists and compilations, not engagement prompts.
- **Mislabelling has real penalties.**
  - In Sept 2025 Disney agreed to pay **$10 M** to settle an FTC/DOJ COPPA complaint over child-directed YouTube videos it had not marked MFK ([NBC News](https://www.nbcnews.com/business/business-news/disney-pay-10-million-settle-ftc-complaint-collection-childrens-person-rcna228786)).
  - Google/YouTube paid **$170 M** in 2019 ([FTC](https://www.ftc.gov/news-events/news/press-releases/2019/09/google-youtube-will-pay-record-170-million-alleged-violations-childrens-privacy-law)). Since then, channel owners themselves can be liable.

### 3.2 COPPA (US): it applies to us via YouTube's global MFK system

- YouTube applies MFK settings worldwide. US viewers will watch a UK channel, so COPPA exposure exists no matter where we target.
- The amended COPPA Rule has required full compliance since **22 Apr 2026** (doc 03, item 13).
- Our obligations:
  - Classify correctly.
  - Collect **no** personal data from children: no contests, "send us your drawing" calls, or kids' email sign-ups.
  - Run no off-platform tracking links aimed at children.

### 3.3 UK, EU and Australia

| Regime | Who it binds | What it means for us |
|---|---|---|
| **UK ICO Age Appropriate Design Code** (Children's code; 15 standards; statutory code under DPA 2018 s.123) | Online services "likely to be accessed by children" ([ICO](https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/childrens-information/childrens-code-guidance-and-resources/)) | YouTube is the in-scope service for the videos. **We become in scope if we run our own website, app, newsletter or game.** In that case: high privacy by default, no nudge techniques, minimal data, and a data protection impact assessment (DPIA). Recommendation: no child-facing site at launch. |
| **UK Online Safety Act 2023** | Platforms (Ofcom) | It binds YouTube, not us directly. YouTube's resulting child-safety enforcement does apply to our uploads. |
| **EU GDPR Art. 8** | Controllers offering online services directly to children | Parental consent is needed below the digital age of consent: 16 by default, 13–16 by member state (Ireland 16, UK 13). We avoid processing children's data entirely. |
| **EU DSA Art. 28 + Commission guidelines (14 Jul 2025)** | Online platforms accessible to minors ([Taylor Wessing summary](https://www.taylorwessing.com/en/insights-and-events/insights/2025/07/rd-european-commission-guidelines-on-protection-of-minors-under-the-digital-services-act)) | They bind YouTube. They push platforms toward stricter minor protections and against exploiting children's commercial naivety, so expect tougher kids-content moderation. **No commercial pressure in our content.** |
| **EU AI Act Art. 50** (transparency, applies from 2 Aug 2026) | AI providers and deployers of deepfakes | Fully animated, non-realistic cartoons are not deepfakes. Keep the YouTube disclosure policy anyway (§3.5). *Verify how it applies to synthetic singing voices.* |
| **Australia: Online Safety Act 2021 + Social Media Minimum Age** (from 10 Dec 2025) | Platforms; eSafety | **YouTube is included**: under-16s in AU can't hold accounts, but logged-out viewing continues ([NBC News](https://nbcnews.com/news/amp/rcna247124)). Toddlers watch on a parent's account, logged out, or in YouTube Kids. Expect AU parental controls not to apply to logged-out viewing. Content must be safe without them. |
| **Australia: Privacy Act, Children's Online Privacy Code** | APP entities offering online services likely accessed by children | Exposure draft released 31 Mar 2026; must be registered by **10 Dec 2026**; commencement date unconfirmed ([Corrs](https://corrs.com.au/insights/oaics-childrens-online-privacy-exposure-draft-from-consultation-to-code)). Same stance: collect no children's data. |
| **Meta (Instagram/Facebook)** | Under-13s cannot hold accounts | **Don't post the kids songs to IG or FB.** At most, run adult-directed behind-the-scenes posts, assessed separately (19). |

### 3.4 YouTube's 2025–2026 crackdown on AI and low-quality kids content

- **YPP "inauthentic content" (15 Jul 2025).** YouTube renamed "repetitious content" to "inauthentic content". Monetised channels must "not be mass-produced, generic, repetitive", and content "made with a template" is ineligible ([YT 1311392](https://support.google.com/youtube/answer/1311392)). YouTube said AI-*assisted* original work stays eligible (press coverage, e.g. [BetaNews](https://betanews.com/2025/07/10/youtube-is-fighting-ai-slop-with-new-monetization-guidelines/)). The reused-content policy applies to the **whole channel**.
- **Kids quality principles** ([YT 10774223](https://support.google.com/youtube/answer/10774223)):
  - **High quality:** looking after yourself and others; learning and inspiring curiosity; creativity, play and imagination; life skills and experiences; shows them the world.
  - **Low quality:** heavily promotional; encourages negative behaviour; deceptively educational; *hard to follow* (YouTube notes this is often the result of "mass production or autogeneration"); sensational or misleading (including keyword stuffing); strange use of children's characters.
  - **Consequences:** high-quality MFK content is "raised up" in recommendations. The principles decide YouTube Kids inclusion and monetisation. A channel focused on low-quality MFK content **may be suspended from YPP**, and individual videos can get limited or no ads.
- **Reported enforcement:**
  - **May 2025:** WIRED identified 70+ channels of disturbing content in child-friendly packaging. YouTube said it terminated two channels, suspended monetisation on three, and removed videos ([summary](https://mezha.ua/en/news/youtube-shows-kids-ai-videos-308931/)).
  - **26 Feb 2026, NYT:** a review of 1,000+ videos recommended to young children found incoherent AI toddler clips of 30 s or less. After the NYT contacted it, YouTube suspended **five channels** from YPP, which also blocked them from YouTube Kids, and removed videos ([The Nightly reprint, 20 Mar 2026](https://thenightly.com.au/society/technology/the-new-york-times-how-generated-ai-videos-are-distorting-your-childs-youtube-feed-c-21998661)).
  - **Feb 2026:** a study reported recommendations pushing low-quality AI videos to toddlers ([OECD AI incident](https://oecd.ai/en/incidents/2026-02-26-abd8)).
  - **2026:** 200+ child-advocacy groups led by Fairplay asked YouTube to ban AI "slop" from kids' surfaces. Fairplay estimated top AI kids channels earn over $4.25 M a year (secondary reporting; *unverified*). Coverage also reports YouTube saying it limits AI content in YouTube Kids to a small set of high-quality channels (*unverified wording*).
  - **Collateral damage:** some AI-assisted animation channels also lost monetisation ([Creator Handbook](https://www.creatorhandbook.net/youtube-demonitizes-popular-animated-channels/)).
- **What this means for us:**
  - Volume and templating are the riskiest moves we could make.
  - Every song needs a real narrative (a beginning, a middle, an end and a takeaway), coherent visuals and consistent characters.
  - **No 30-second autogenerated clips as the core product.**
  - Low cadence and high craft.

### 3.5 Altered or synthetic content disclosure

YouTube requires disclosure for **realistic** AI content. It says creators "don't need to disclose non-realistic content that's made with AI", and gives as an example an AI animation in a fully animated video ([YT 14328491](https://support.google.com/youtube/answer/14328491)). A stylised 3D cartoon therefore probably **does not require** the label.

**Our rule stays: turn the AI disclosure on anyway** (AGENTS.md). It costs nothing, matches the brand pack, and protects us if a synthetic singing voice or realistic background were ever judged "realistic".

### 3.6 YouTube Kids app eligibility

- There is no opt-in. Automated systems plus some human review select content, and "not all videos have been manually reviewed" ([YT 10938174](https://support.google.com/youtube/answer/10938174)).
- The **Preschool** setting (age 4 and under) explicitly lists nursery rhymes, cartoons, read-alongs and circle time.
- **Excluded:** overly commercial content, packaging focus, branded content, and deceptive or sensational titles and thumbnails, including keyword stuffing and "unrelated kids' themes mashed together".
- Inclusion also follows the quality principles (§3.4), and a YPP suspension blocks it (NYT case).
- **Don't plan on YouTube Kids traffic.** Earn it.

### 3.7 Repo state

- `assert_publishable` requires `made_for_kids=False`, and AGENTS.md says to set "Not made for kids" on every video.
- **For this channel that setting would be a mislabel.** So this channel stays **local and unpublished** until a human deliberately implements and reviews kids publishing, keeping approval, hash binding and `DRY_RUN` (21).
- Never mark it "not made for kids" to get past the gate.

---

## 4. Monetisation reality (MFK, UK/IE/EU/AU)

- **YPP entry today:** 1,000 subscribers plus either 4,000 public watch hours in 12 months, or 10 M Shorts views in 90 days.
- **Announced from 1 Feb 2027:** 8,000 hours or 20 M Shorts views (19, [YT 12843009](https://support.google.com/youtube/answer/12843009)). Plan on the 2027 bar.
- **MFK-specific limits:**
  - Contextual ads only.
  - No memberships, Super Chat, merch shelf or donate button (§3.1), so fan-funding YPP tiers are largely useless here.
  - **Shorts revenue for MFK is unverified.** YouTube's Shorts monetisation page doesn't mention MFK ([YT 12504220](https://support.google.com/youtube/answer/12504220)). vidIQ says MFK Shorts can earn Shorts revenue sharing (secondary). Assume $0 until Studio shows otherwise.
  - Ad categories are restricted: no food, toys-as-games and so on.
- **RPM (estimates only).**
  - No primary source publishes MFK RPM by country.
  - A secondary article reports MFK CPMs of about $0.25–0.35 and says revenue drops by more than 95% compared with a not-MFK setting ([pastory.app](https://pastory.app/articles/making-money-youtube-kids-channel/), creator anecdotes, *unverified*).
  - Planning range: **$0.25–$1.00 per 1,000 long-form views**, with $2.00 as an optimistic sensitivity. The same illustrative inputs are used in 19. UK and AU are generally richer ad markets than the global average, but measure before forecasting.
- **Scale check.**
  - Moonbug reports more than 4 B monthly views for CoComelon alone, about 133 M a day. Pinkfong did 66.5 B in 2025 across its whole company.
  - **1 B lifetime views** is top-tier franchise territory.
  - **1 B per month** is CoComelon-and-Pinkfong scale, reached with studios, years and dozens of languages.
  - Even at 1 B views, $0.25–$1.00 RPM is about **$250 k–$1 M**, before production costs.

**Staged targets** (our planning assumptions, not forecasts; most new kids channels never reach YPP):

| Stage | Time from first public upload | Output | Views (cumulative) | Gate to move on |
|---|---|---|---|---|
| 0 Pilot | before launch | 3 finished songs + 1 compilation, local only | — | 5 caregiver reviewers + 1 early-years educator approve; rights file complete; kids publishing implemented and reviewed |
| 1 Launch | 0–3 months | 12 songs, 3 compilations, ~30 Shorts | 10 k–100 k | Average % viewed on singles ≥ the channel's own first-month median; no policy strikes |
| 2 Traction | 3–9 months | 30 songs, 10 compilations | 100 k–2 M | YPP eligibility (8 k h under 2027 rules) — the stretch goal |
| 3 Monetised | 9–18 months | 50+ songs | 2 M–20 M | Measured RPM; views per production hour rising |
| 4 Breakout (stretch, low probability) | 18–36 months | 100+ songs, a second language channel | 100 M+ | A hit single and repeatable compilation demand |
| "1 B views" | multi-year | franchise | 1 B lifetime | Only with a breakout IP and studio-level investment |

---

## 5. Recommended format for the first channel

### 5.1 Brand concept requirements (naming is left to the creative agent; see the doc 27 draft in progress)

- **An original world** that can't be mistaken for any competitor:
  - Its own species or family design. Using an animal family avoids the lookalike human-toddler problem (the CoComelon/JJ risk) and the realistic-child disclosure question.
  - Its own silhouette, palette and logo. No melon or fruit, shark, fox, star mascot or baby-bum motif.
- **A UK/Commonwealth home setting:** a terraced or semi-detached house, a garden, rain and wellies, a park, the seaside, a corner shop. Avoid a US suburb.
- **A cast:** a toddler lead (an animal), an older sibling, mum and dad or other carers, a grandparent, a pet, and two friend families of different kinds. There must always be an adult present in bath, water, food and stairs scenes.
- **A name** that is short, singable, original and free of competitor words (melon, shark, finn, bum, simple). Clear trademarks (UKIPO, EUIPO, IP Australia) and handles before committing.
- **A voice:** a warm British/Commonwealth female or male lead, plus a child-like character voice. Hire real singers, or use only licensed synthetic voices that are never clones of a real person.

### 5.2 Episode spec

| Item | Spec |
|---|---|
| Single | 2:00–3:00 (≈ 4–6 verses × 20–30 s); one routine or concept; verse/chorus with one variable per verse; a closing calm-down bar |
| Tempo | Measure reference tracks first; working assumption 90–110 BPM for routines, 60–75 BPM for bedtime |
| Camera | Slow moves, shots held ≥ 4 s, no flashing or strobing, no sudden zooms or loud stings; soft audio (−14 LUFS master, and check perceived loudness) |
| Visual | 3D toy/clay look, consistent character models, no text in images (overlays only) |
| Learning hook | One clear takeaway per song (e.g. brush the top and bottom teeth), per the quality principles |
| Short | 20–40 s chorus cut, opening on the familiar moment |

**Production gap (honest).**
- The current stack (Z-Image stills + 2.5D parallax + Kokoro speech) **cannot** make CoComelon-style continuous 3D character animation, lip-sync or singing (21).
- A real launch needs things outside the current pipeline:
  - a rigged 3D cast, built in Blender or bought;
  - an animation workflow;
  - a composer, singer and mixing workflow.
- Stills-with-parallax singalongs would sit near the "hard to follow / mass-produced" end of the market.
- **Decide this before committing to the format.**

### 5.3 Cadence and compilation plan

- **Launch bank:** 8 songs finished and reviewed before the first upload.
- **Then:** 1 new song a week (alternating original and re-imagined PD), 1 new themed 30–45 min compilation every 2 weeks once 8 songs exist, and 3 Shorts a week cut from approved songs.
- **From month 3:** a 60 min "Bath & Bedtime" compilation, refreshed monthly as songs are added.
- **Playlists:** Routines, Animals, Counting & Colours, Bedtime, and New Songs. Order them so autoplay flows.
- **Seasonal pieces** for UK/AU calendars, in the right hemisphere: Christmas in summer for AU, Bonfire Night (no fireworks danger), Easter, the first day at nursery, beach safety for AU.
- **Do not scale cadence** beyond what the human review checklist can cover properly.

### 5.4 Human review checklist (every video)

1. **Classification:** MFK is set at channel and video level. AI disclosure is on. No made_for_kids override by an agent. A human approves (`studio approve` is human-only).
2. **Originality:** the melody, lyrics and characters don't resemble any competitor's (do a listen-through comparison and a reverse image search of key frames). No competitor names or keywords in the title, tags or description.
3. **Rights file:** composition, lyrics, master, performers, AI-tool terms, the PD basis for traditional rhymes, and the stock licences.
4. **Coherence:** the story has a beginning, a middle, an end and a takeaway. Characters stay on model. No malformed hands, faces or objects. No AI text artefacts.
5. **Safety:** no dangerous imitable acts (standing in the bath, small parts, cords, stairs or water without an adult). No scary content. No shaming. Nappy and potty scenes handled discreetly.
6. **Pacing and sensory:** no flashing or strobing (check against the WCAG 3-flashes-per-second rule). Average shot ≥ 4 s. Loudness checked.
7. **Language:** UK spelling and vocabulary. Pronunciation checked. Lyrics readable at toddler pace.
8. **Commercial:** no products, brands, toys, "ask your mum to buy", "subscribe" or "watch to the end" prompts.
9. **Claims:** no health, sleep-training or developmental claims. Educational claims in the title must be delivered by the video.
10. **Packaging:** the title is plain and descriptive, and the thumbnail is honest, not sensational, with no misleading characters.
11. **Audience note:** WHO 2019 guidance advises no screen time under 2 and at most 1 h a day for ages 2–4 ([coverage](https://www.wbur.org/hereandnow/2019/04/25/screen-time-kids-guidelines)). Position the channel for **ages 2–4 with a caregiver**, and never market it as "for babies" or as a babysitter.
12. **Data:** no calls for children to send anything, and no links to data collection.

---

## Sources

The primary sources are cited inline: YouTube Help pages 9528076, 9632097, 9713557, 10774223, 10938174, 1311392, 12504220, 14328491 and 12843009; the Moonbug press release; the FTC; the ICO; EUR-Lex; the Lillard & Peterson study; and the Trehub/Trainor papers. Secondary or third-party sources are marked where they are used: Wikipedia, Social Blade, vidIQ, press reprints, law-firm summaries and pastory.app. Recheck every dated figure before any external use.
