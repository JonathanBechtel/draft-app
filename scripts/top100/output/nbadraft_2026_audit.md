# 2026 First Round Audit — nbadraft.net

Source CSV: `scripts/top100/output/nbadraft_2026_first_round.csv`  
Database: Neon dev branch (from `.env`)

## Summary

| Pick | Player | DB matches | Ref image | Stylized | Combine | Dupes |
|---:|---|---:|:---:|:---:|:---:|:---:|
| 1 | AJ Dybantsa | 1 | ✅ | ✅ | ✅ | — |
| 2 | Darryn Peterson | 1 | ✅ | ✅ | ✅ | — |
| 3 | Cameron Boozer | 1 | ✅ | ✅ | ✅ | ⚠️ 2 |
| 4 | Caleb Wilson | 1 | ✅ | ✅ | ✅ | — |
| 5 | Darius Acuff | 1 | ✅ | ✅ | ✅ | — |
| 6 | Keaton Wagler | 1 | ✅ | ✅ | ✅ | ⚠️ 1 |
| 7 | Kingston Flemings | 1 | ❌ | ✅ | ✅ | — |
| 8 | Mikel Brown | 1 | ❌ | ✅ | ❌ | ⚠️ 5 |
| 9 | Nate Ament | 1 | ✅ | ✅ | ❌ | ⚠️ 1 |
| 10 | Hannes Steinbach | 1 | ❌ | ✅ | ✅ | — |
| 11 | Brayden Burries | 1 | ✅ | ✅ | ✅ | — |
| 12 | Labaron Philon | 1 | ❌ | ✅ | ✅ | — |
| 13 | Yaxel Lendeborg | 1 | ✅ | ✅ | ✅ | ⚠️ 1 |
| 14 | Cameron Carr | 1 | ✅ | ✅ | ✅ | ⚠️ 1 |
| 15 | Chris Cenac | 1 | ❌ | ✅ | ❌ | ⚠️ 2 |
| 16 | Karim Lopez | 1 | ❌ | ✅ | ✅ | — |
| 17 | Isaiah Evans | 1 | ❌ | ✅ | ✅ | ⚠️ 1 |
| 18 | Meleek Thomas | 1 | ❌ | ✅ | ✅ | ⚠️ 4 |
| 19 | Allen Graves | 1 | ✅ | ✅ | ✅ | — |
| 20 | Bennett Stirtz | 1 | ❌ | ✅ | ✅ | — |
| 21 | Morez Johnson | 1 | ✅ | ✅ | ✅ | ⚠️ 8 |
| 22 | Joshua Jefferson | 1 | ❌ | ✅ | ✅ | — |
| 23 | Christian Anderson | 1 | ✅ | ✅ | ✅ | ⚠️ 2 |
| 24 | Jayden Quaintance | 1 | ❌ | ✅ | ✅ | — |
| 25 | Aday Mara | 1 | ✅ | ✅ | ✅ | — |
| 26 | Dailyn Swain | 1 | ❌ | ✅ | ✅ | — |
| 27 | Koa Peat | 1 | ✅ | ✅ | ✅ | ⚠️ 1 |
| 28 | Milan Momcilovic | 1 | ✅ | ✅ | ✅ | — |
| 29 | Ebuka Okorie | 1 | ❌ | ✅ | ✅ | — |
| 30 | Tyler Tanner | 1 | ✅ | ✅ | ✅ | — |

## Per-prospect detail

### 1. AJ Dybantsa — BYU (SF, Fr.) → Washington

- **player_id=5390** `aj-dybantsa` — display: `AJ Dybantsa` (draft_year=2026)
    - aliases: AJ Dybantsa || Anicet Dybantsa
    - reference image: ✅  (url:https://upload.wikimedia.org/wikipedia/commons/d/da/AJ_Dybantsa_2024.jpg)
    - stylized images: ✅ (1 asset(s), styles: default)
    - combine: anthro=1, agility=1

### 2. Darryn Peterson — Kansas (SG, Fr.) → Utah

- **player_id=5466** `darryn-peterson` — display: `Darryn Peterson` (draft_year=2026)
    - aliases: Darryn Peterson
    - reference image: ✅ (s3:reference-images/5466_darryn-peterson.png)
    - stylized images: ✅ (1 asset(s), styles: default)
    - combine: anthro=1, agility=1

### 3. Cameron Boozer — Duke (PF, Fr.) → Memphis

- **player_id=5653** `cameron-boozer` — display: `Cameron Boozer` (draft_year=2026)
    - aliases: Cameron Boozer
    - reference image: ✅  (url:https://upload.wikimedia.org/wikipedia/commons/0/0d/Boozer_%E2%80%93_Clemson_%E2%80%93_February_14_2026_%28cropped%29.png)
    - stylized images: ✅ (1 asset(s), styles: default)
    - combine: anthro=1, agility=1

  ⚠️ **Potential duplicate row(s) in DB** — review for merge:
    - player_id=5623 `cam-boozer` — `Cam Boozer` draft_year=2026 school='Duke' (stub)
    - player_id=5801 `cayden-boozer` — `Cayden Boozer` draft_year=2026 school='Duke' (stub)

### 4. Caleb Wilson — North Carolina (PF/C, Fr.) → Chicago

- **player_id=5469** `caleb-wilson` — display: `Caleb Wilson` (draft_year=2026)  ⚠️ school mismatch (DB: `UNC`)
    - aliases: Caleb Wilson
    - reference image: ✅  (url:https://upload.wikimedia.org/wikipedia/commons/9/9c/Caleb_Wilson.jpg)
    - stylized images: ✅ (1 asset(s), styles: default)
    - combine: anthro=1, agility=1

### 5. Darius Acuff — Arkansas (PG, Fr.) → LA Clippers

- **player_id=5384** `darius-acuff-jr` — display: `Darius Acuff Jr.` (draft_year=2026)
    - aliases: Darius Acuff || Darius Acuff Jr || Darius Acuff Jr.
    - reference image: ✅ (s3:reference-images/5384_darius-acuff-jr.png)
    - stylized images: ✅ (3 asset(s), styles: default)
    - combine: anthro=1, agility=1

### 6. Keaton Wagler — Illinois (PG/SG, Fr.) → Brooklyn

- **player_id=5690** `keaton-wagler` — display: `Keaton Wagler` (draft_year=2026)
    - aliases: Keaton Wagler
    - reference image: ✅ (s3:reference-images/5690_keaton-wagler.png)
    - stylized images: ✅ (1 asset(s), styles: default)
    - combine: anthro=1, agility=1

  ⚠️ **Potential duplicate row(s) in DB** — review for merge:
    - player_id=6039 `wagler` — `Wagler` draft_year=2026 school='Illinois' (stub)

### 7. Kingston Flemings — Houston (PG, Fr.) → Sacramento

- **player_id=5708** `kingston-flemings` — display: `Kingston Flemings` (draft_year=2026)
    - aliases: Kingston Flemings
    - reference image: ❌ 
    - stylized images: ✅ (1 asset(s), styles: default)
    - combine: anthro=1, agility=1

### 8. Mikel Brown — Louisville (PG, Fr.) → Atlanta

- **player_id=5389** `mikel-brown-jr` — display: `Mikel Brown Jr.` (draft_year=2026)
    - aliases: Mikel Brown || Mikel Brown Jr.
    - reference image: ❌ 
    - stylized images: ✅ (2 asset(s), styles: default)
    - combine: ❌ none

  ⚠️ **Potential duplicate row(s) in DB** — review for merge:
    - player_id=5832 `tevin-brown` — `Tevin Brown` draft_year=2022 school='Murray State' (stub)
    - player_id=6058 `maliq-brown` — `Maliq Brown` draft_year=2026 school='Duke' (stub)
    - player_id=6146 `christopher-brown-jr` — `Christopher Brown Jr` draft_year=2026 school=None
    - player_id=5717 `aj-brown` — `AJ Brown` draft_year=2027 school='Florida' (stub)
    - player_id=5830 `gabe-brown` — `Gabe Brown` draft_year=2022 school='Michigan State' (stub)

### 9. Nate Ament — Tennessee (SF/PF, Fr.) → Dallas

- **player_id=5561** `nate-ament` — display: `Nate Ament` (draft_year=2026)
    - aliases: Nate Ament
    - reference image: ✅ (s3:reference-images/5561_nate-ament.png)
    - stylized images: ✅ (1 asset(s), styles: default)
    - combine: ❌ none

  ⚠️ **Potential duplicate row(s) in DB** — review for merge:
    - player_id=6143 `nathaniel-ament` — `Nathaniel Ament` draft_year=2026 school=None

### 10. Hannes Steinbach — Washington (PF/C, Fr.) → Milwaukee

- **player_id=5456** `hannes-steinbach` — display: `Hannes Steinbach` (draft_year=2026)
    - aliases: Hannes Steinbach
    - reference image: ❌ 
    - stylized images: ✅ (1 asset(s), styles: default)
    - combine: anthro=1, agility=1

### 11. Brayden Burries — Arizona (PG/SG, Fr.) → Golden St.

- **player_id=5597** `brayden-burries` — display: `Brayden Burries` (draft_year=2026)  🔸stub
    - aliases: Brayden Burries
    - reference image: ✅  (url:https://upload.wikimedia.org/wikipedia/commons/5/5c/Brayden_Burries.jpg)
    - stylized images: ✅ (1 asset(s), styles: default)
    - combine: anthro=1, agility=1

### 12. Labaron Philon — Alabama (PG, So.) → Oklahoma Cty

- **player_id=1709** `labaron-philon` — display: `Labaron Philon` (draft_year=2026)
    - aliases: Labaron Philon || Labaron Philon Jr.
    - reference image: ❌ 
    - stylized images: ✅ (2 asset(s), styles: default)
    - combine: anthro=2, agility=2, shooting=1

### 13. Yaxel Lendeborg — Michigan (PF, Sr.) → Miami

- **player_id=1691** `yaxel-lendeborg` — display: `Yaxel Lendeborg` (draft_year=2025)
    - aliases: Yaxel Lendeborg
    - reference image: ✅  (url:https://upload.wikimedia.org/wikipedia/commons/8/8e/20260211_Yaxel_Lendeborg_05.jpg)
    - stylized images: ✅ (1 asset(s), styles: default)
    - combine: anthro=2, agility=2, shooting=1

  ⚠️ **Potential duplicate row(s) in DB** — review for merge:
    - player_id=6040 `lendeborg` — `Lendeborg` draft_year=2026 school='Michigan' (stub)

### 14. Cameron Carr — Baylor (SG, Jr.) → Charlotte

- **player_id=5519** `cameron-carr` — display: `Cameron Carr` (draft_year=2026)
    - aliases: Cameron Carr
    - reference image: ✅  (url:https://images.sidearmdev.com/crop?url=https%3A%2F%2Fdxbhsrqyrr690.cloudfront.net%2Fsidearm.nextgen.sites%2Fbaylorbears.com%2Fimages%2F2025%2F10%2F14%2FCarr_Cameron.png&width=180&height=270&type=webp)
    - stylized images: ✅ (1 asset(s), styles: default)
    - combine: anthro=1, agility=1

  ⚠️ **Potential duplicate row(s) in DB** — review for merge:
    - player_id=5650 `coen-carr` — `Coen Carr` draft_year=2026 school='Michigan State' (stub)

### 15. Chris Cenac — Houston (PF/C, Fr.) → Chicago

- **player_id=5387** `chris-cenac-jr` — display: `Chris Cenac Jr.` (draft_year=2026)
    - aliases: Chris Cenac || Chris Cenac Jr.
    - reference image: ❌ 
    - stylized images: ✅ (2 asset(s), styles: default)
    - combine: ❌ none

  ⚠️ **Potential duplicate row(s) in DB** — review for merge:
    - player_id=6147 `christopher-cenac-jr` — `Christopher Cenac Jr.` draft_year=2026 school=None
    - player_id=5662 `shelton-cenac` — `Shelton Cenac` draft_year=2026 school='Houston' (stub)

### 16. Karim Lopez — Mexico (SF/PF, Intl.) → Memphis

- **player_id=5605** `karim-lopez` — display: `Karim Lopez` (draft_year=2026)  🔸stub  ⚠️ school mismatch (DB: `NZ Breakers`)
    - aliases: Karim Lopez
    - reference image: ❌ 
    - stylized images: ✅ (1 asset(s), styles: default)
    - combine: anthro=1, agility=1

### 17. Isaiah Evans — Duke (SG/SF, So.) → Oklahoma Cty

- **player_id=5509** `isaiah-evans` — display: `Isaiah Evans` (draft_year=2026)  🔸stub
    - aliases: Isaiah Evans
    - reference image: ❌ 
    - stylized images: ✅ (1 asset(s), styles: default)
    - combine: anthro=1, agility=1

  ⚠️ **Potential duplicate row(s) in DB** — review for merge:
    - player_id=5640 `kwame-evans-jr` — `Kwame Evans Jr.` draft_year=2026 school='Oregon' (stub)

### 18. Meleek Thomas — Arkansas (PG/SG, Fr.) → Charlotte

- **player_id=5647** `meleek-thomas` — display: `Meleek Thomas` (draft_year=2026)  🔸stub
    - aliases: Meleek Thomas
    - reference image: ❌ 
    - stylized images: ✅ (1 asset(s), styles: default)
    - combine: anthro=1, agility=1

  ⚠️ **Potential duplicate row(s) in DB** — review for merge:
    - player_id=5521 `saint-thomas` — `Saint Thomas` draft_year=2025 school='USC' (stub)
    - player_id=5678 `thomas-dowd` — `Thomas Dowd` draft_year=None school=None (stub)
    - player_id=1721 `thomas-sorber` — `Thomas Sorber` draft_year=2025 school=None
    - player_id=5379 `thomas-haugh` — `Thomas Haugh` draft_year=2026 school='Florida'

### 19. Allen Graves — Santa Clara (PF, Fr.) → Toronto

- **player_id=5563** `allen-graves` — display: `Allen Graves` (draft_year=2026)  🔸stub
    - aliases: Allen Graves
    - reference image: ✅  (url:https://upload.wikimedia.org/wikipedia/commons/c/cd/The_New_York_Evangelist_1839-10-12-_Vol_10_Iss_41_%28IA_sim_evangelist-and-religious-review_1839-10-12_10_41%29.pdf)
    - stylized images: ✅ (1 asset(s), styles: default)
    - combine: anthro=1, agility=1

### 20. Bennett Stirtz — Iowa (PG, Sr.) → San Antonio

- **player_id=5578** `bennett-stirtz` — display: `Bennett Stirtz` (draft_year=2026)  🔸stub
    - aliases: Benett Stirtz || Bennett Stirtz
    - reference image: ❌ 
    - stylized images: ✅ (1 asset(s), styles: default)
    - combine: anthro=1, agility=1

### 21. Morez Johnson — Michigan (PF/C, So.) → Detroit

- **player_id=5529** `morez-johnson-jr` — display: `Morez Johnson Jr.` (draft_year=2026)  🔸stub
    - aliases: Morez Johnson || Morez Johnson Jr || Morez Johnson Jr.
    - reference image: ✅  (url:https://upload.wikimedia.org/wikipedia/commons/f/fd/20260211_Morez_Johnson_Jr._and_Jayden_Reid.jpg)
    - stylized images: ✅ (3 asset(s), styles: default)
    - combine: anthro=1, agility=1

  ⚠️ **Potential duplicate row(s) in DB** — review for merge:
    - player_id=5902 `cam-johnson` — `Cam Johnson` draft_year=2019 school='UNC' (stub)
    - player_id=5596 `isaiah-johnson` — `Isaiah Johnson` draft_year=2026 school='Colorado' (stub)
    - player_id=5481 `brandon-johnson` — `Brandon Johnson` draft_year=2025 school='Miami (FL)' (stub)
    - player_id=5714 `london-johnson` — `London Johnson` draft_year=2030 school='Louisville' (stub)
    - player_id=5879 `dior-johnson` — `Dior Johnson` draft_year=2027 school='Tarleton State' (stub)
    - player_id=1623 `aj-johnson` — `AJ Johnson` draft_year=2024 school=None
    - player_id=1686 `tre-johnson` — `Tre Johnson` draft_year=2025 school='Texas'
    - player_id=5654 `kobe-johnson` — `Kobe Johnson` draft_year=2025 school='UCLA' (stub)

### 22. Joshua Jefferson — Iowa St. (SF, Sr.) → Philadelphia

- **player_id=5388** `joshua-jefferson` — display: `Joshua Jefferson` (draft_year=2026)  ⚠️ school mismatch (DB: `Iowa State`)
    - aliases: Joshua Jefferson
    - reference image: ❌ 
    - stylized images: ✅ (1 asset(s), styles: default)
    - combine: anthro=1, agility=1

### 23. Christian Anderson — Texas Tech (PG, So.) → Atlanta

- **player_id=5382** `christian-anderson` — display: `Christian Anderson` (draft_year=2026)
    - aliases: Christian Anderson
    - reference image: ✅  (url:https://upload.wikimedia.org/wikipedia/commons/6/63/Christian_Anderson_Jr._March_Madness.jpg)
    - stylized images: ✅ (1 asset(s), styles: default)
    - combine: anthro=1, agility=1

  ⚠️ **Potential duplicate row(s) in DB** — review for merge:
    - player_id=6082 `nasir-anderson` — `Nasir Anderson` draft_year=2027 school=None (stub)
    - player_id=5631 `tucker-anderson` — `Tucker Anderson` draft_year=2027 school='Utah State' (stub)

### 24. Jayden Quaintance — Kentucky (PF/C, So.) → New York

- **player_id=5478** `jayden-quaintance` — display: `Jayden Quaintance` (draft_year=2026)
    - aliases: Jayden Quaintance
    - reference image: ❌ 
    - stylized images: ✅ (1 asset(s), styles: default)
    - combine: anthro=1, agility=1

### 25. Aday Mara — Michigan (C, Jr.) → LA Lakers

- **player_id=5577** `aday-mara` — display: `Aday Mara` (draft_year=2026)  🔸stub
    - aliases: Aday Mara
    - reference image: ✅  (url:https://upload.wikimedia.org/wikipedia/commons/8/8f/20260211_Aday_Mara_05.jpg)
    - stylized images: ✅ (1 asset(s), styles: default)
    - combine: anthro=1, agility=1

### 26. Dailyn Swain — Texas (SF, Jr.) → Denver

- **player_id=5465** `dailyn-swain` — display: `Dailyn Swain` (draft_year=2026)  🔸stub
    - aliases: Dailyn Swain
    - reference image: ❌ 
    - stylized images: ✅ (1 asset(s), styles: default)
    - combine: anthro=1, agility=1

### 27. Koa Peat — Arizona (PF, Fr.) → Boston

- **player_id=5513** `koa-peat` — display: `Koa Peat` (draft_year=2026)
    - aliases: Koa Peat
    - reference image: ✅  (url:https://upload.wikimedia.org/wikipedia/commons/b/bc/Koa_Peat.jpg)
    - stylized images: ✅ (1 asset(s), styles: default)
    - combine: anthro=1, agility=1

  ⚠️ **Potential duplicate row(s) in DB** — review for merge:
    - player_id=6042 `peat` — `Peat` draft_year=2026 school='Arizona' (stub)

### 28. Milan Momcilovic — Iowa St. (SF/PF, Jr.) → Minnesota

- **player_id=5380** `milan-momcilovic` — display: `Milan Momcilovic` (draft_year=2026)  🔸stub  ⚠️ school mismatch (DB: `Iowa State`)
    - aliases: Milan Momcilovic
    - reference image: ✅  (url:https://upload.wikimedia.org/wikipedia/commons/0/07/MBB-IowaSt-FloridaA%26M-Momcilovic.jpg)
    - stylized images: ✅ (1 asset(s), styles: default)
    - combine: anthro=1, agility=1

### 29. Ebuka Okorie — Stanford (PG, Fr.) → Cleveland

- **player_id=5674** `ebuka-okorie` — display: `Ebuka Okorie` (draft_year=2026)  🔸stub
    - aliases: Ebuka Okorie
    - reference image: ❌ 
    - stylized images: ✅ (1 asset(s), styles: default)
    - combine: anthro=1, agility=1

### 30. Tyler Tanner — Vanderbilt (PG, So.) → Dallas

- **player_id=5679** `tyler-tanner` — display: `Tyler Tanner` (draft_year=2026)  🔸stub
    - aliases: Tyler Tanner
    - reference image: ✅  (url:https://upload.wikimedia.org/wikipedia/commons/a/a7/Tyler_Tanner_Evergreen_2018.jpg)
    - stylized images: ✅ (1 asset(s), styles: default)
    - combine: anthro=1, agility=1

