# Application comment audit

Original line numbers refer to `17ac60c`. The audit covers 81 tracked Python files under `app/`, including 48 with comment tokens. Each range includes individual lines of a multiline comment.

| Category | Standalone | Inline | Total |
| --- | ---: | ---: | ---: |
| Tautology | 208 | 48 | 256 |
| Stale | 27 | 4 | 31 |
| Excuse | 14 | 3 | 17 |
| Decision record | 463 | 17 | 480 |
| Tool directive | 0 | 166 | 166 |
| Prose tokens removed | 712 | 72 | 784 |

A further 29 inline directive tokens carried prose suffixes. The audit removed their suffixes and kept the operative directive text. Python string literals and docstrings remain intact.

## Decisions and issues

- 0001 [Delivery receipts and worker recovery](../adr/0001-application-delivery-and-recovery.md)
- 0002 [Resource ownership through cancellation](../adr/0002-application-resource-ownership.md)
- 0003 [Exact media identity](../adr/0003-application-media-identity.md)
- 0004 [Provider boundaries and fallback](../adr/0004-application-provider-boundaries.md)
- 0005 [Telegram media compatibility](../adr/0005-application-telegram-media-compatibility.md)
- 0006 [Runtime and ephemeral state](../adr/0006-application-runtime-state.md)
- 0007 [Callback and retry state](../adr/0007-application-callback-state.md)
- 0008 [GIF source binding](../adr/0008-application-gif-source-binding.md)
- E1 [Reject cloud uploads when local file stat fails](https://github.com/Le-Who/ytdlbot/issues/147)
- E2 [Replace private CPython process-handle bridge](https://github.com/Le-Who/ytdlbot/issues/149)
- E3 [Continue Cobalt failover on unsupported status](https://github.com/Le-Who/ytdlbot/issues/148)
- E4 [Invalidate TikWM CDN aliases together](https://github.com/Le-Who/ytdlbot/issues/150)

## Original lines by file

Parenthesized entries in the decision and excuse columns identify the record or issue above.

| File | Tautology | Stale | Excuse | Decision record | Tool directive |
| --- | --- | --- | --- | --- | --- |
| `app/api/routes.py` | 361 | — | — | 190–191, 557 (1)<br>432–433, 544–545 (2)<br>268, 368 (5) | 101, 161, 237, 397, 401–402, 407, 414, 449, 500, 521, 546 |
| `app/bot/callback_work.py` | — | — | — | 27 (7) | — |
| `app/bot/callbacks.py` | 158, 815, 844, 871, 874, 946, 953, 993, 1011, 1059, 1067, 1089, 1126 | 1018 | — | 125, 155, 162, 208, 279, 281, 302, 436, 490, 521, 533, 636, 949, 970, 977, 1006, 1027, 1034, 1043, 1063, 1084, 1113, 1139, 1144–1145, 1151 (1)<br>922–923, 1092–1093 (2)<br>545, 611–612, 913, 941–942, 974, 1012–1013, 1021, 1048, 1118 (8) | 126, 156, 163, 209, 280, 303, 437, 489, 491, 522, 534, 553, 567, 575, 586, 618, 637, 910, 950, 971, 1007, 1028, 1035, 1044, 1064, 1085, 1094, 1114, 1140, 1152 |
| `app/bot/commands.py` | 49, 57, 70, 86, 199, 229, 248, 263 | — | — | 106, 153, 184 (1)<br>39 (7) | 107, 152, 154, 185 |
| `app/bot/format_formatter.py` | 16, 24, 39 | — | — | — | — |
| `app/bot/group_logic.py` | 40, 79, 82, 88, 226, 229, 233, 289, 295, 302, 337–338, 667, 740 | 66–67, 76, 786 | — | 188, 193, 249, 263, 284, 343, 452, 465, 492, 533, 550, 557, 619, 673, 697, 718, 733, 756, 779 (1)<br>267, 292, 375, 659 (4)<br>61–62 (7)<br>499, 787 (8) | 189, 194, 250, 264, 285, 344, 453, 466, 493, 534, 551, 558, 620, 674, 698, 719, 734, 757, 780 |
| `app/bot/ig_callbacks.py` | 179, 197, 214, 329, 363 | — | — | 140, 237, 471, 483, 499, 582, 601, 656, 673, 751 (1)<br>345 (7) | 141, 238, 472, 484, 500, 583, 602, 657, 674, 752 |
| `app/bot/messages.py` | 209, 228, 230, 237, 239, 244, 251, 265, 314, 317, 336, 356, 387, 462, 502, 517, 523, 527, 531, 570, 588, 735, 758, 870, 938, 954, 992, 1031, 1038, 1085, 1132 | 1152–1154 | — | 170, 332, 352, 474–475, 560, 666, 706, 716, 781, 831, 863, 1116, 1126, 1170, 1191, 1199, 1208, 1214 (1)<br>1122, 1160–1161 (2)<br>511 (3)<br>359–360, 592 (4)<br>580, 1077, 1155–1156 (5)<br>240–241, 267–268 (7)<br>647 (8) | 171, 333, 353, 561, 667, 707, 717, 782, 832, 864, 1117, 1127, 1162, 1171, 1192, 1200, 1215 |
| `app/bot/retry.py` | — | — | — | 111–112, 118–119, 330, 338 (1)<br>232–233, 271–272 (7) | 120, 331, 339 |
| `app/constants.py` | 3, 7, 29, 34, 38 | — | — | 8–9 (4)<br>4 (6) | — |
| `app/core/config.py` | 13, 326–327, 329, 332 | 156–157, 204–206 | — | 63–65, 126, 155, 161–163, 176, 182, 186, 190, 194–196, 199–201, 311–312, 319–320, 333–335 (6) | — |
| `app/core/download_queue.py` | — | — | — | 106–107, 179–180, 206–207, 277–278 (2) | 279 |
| `app/core/drain.py` | — | — | — | 16–17, 117, 246–247, 281–282, 335–336, 351–352, 367–368, 466–467, 552–553 (1) | 296, 337, 353, 369, 554 |
| `app/core/job_store.py` | — | — | — | 982 (1) | 605, 830 |
| `app/core/limiter.py` | 25 | 66 | — | 63–65, 111, 122–123, 125 (6) | 124 |
| `app/core/metrics.py` | 134, 192, 210, 237, 283, 330, 347 | — | — | — | — |
| `app/core/process.py` | — | — | 171–172 (E2) | 295–296, 312–314, 327–329, 384–386, 634–635, 715–716 (2) | 173, 496, 717 |
| `app/core/resource_budget.py` | — | — | — | 194–195, 226–227, 238–239 (2) | — |
| `app/core/state.py` | 37, 40, 43–44, 46, 52, 55, 58, 61, 63, 78, 84, 108, 127, 136, 139 | — | — | 45, 47–49, 75, 79–80 (6)<br>148 (8) | — |
| `app/core/storage/redis_storage.py` | 30, 65 | — | — | 12, 15, 56, 73–74, 83–84, 93, 96–97 (6) | 75, 85 |
| `app/core/texts.py` | 12, 40, 86, 116–117, 130, 139, 153, 159, 175 | — | — | 148 (5) | — |
| `app/core/user_prefs.py` | 29, 39 | — | — | 63 (6) | — |
| `app/main.py` | 196, 210, 223, 390 | — | — | 77–78, 113–114, 123–124, 202–203, 215, 250–251, 449–450 (1)<br>305 (6) | 79, 204, 252 |
| `app/services/cobalt.py` | 25, 68, 77–78, 80, 84, 261 | — | 187 (E3) | 233–234 (1)<br>282 (2)<br>158–159, 193–194, 300 (4) | 38, 195, 238, 283, 301 |
| `app/services/converter.py` | 21, 27, 140, 182, 284, 302, 311, 334, 355, 367, 370, 510, 651, 789, 799, 850–851, 858, 867, 880 | 42, 45 | — | 337, 452 (2)<br>252–253, 484–485, 702–703 (4)<br>24, 31–32, 35, 38, 41, 44, 504, 508, 669, 734, 881, 914 (5) | 254, 486, 704 |
| `app/services/downloader.py` | 260, 289, 323, 326, 330, 334 | 55 | — | 151–152, 179–181, 267 (2)<br>243 (4)<br>104, 107, 123 (5)<br>39 (6) | 153 |
| `app/services/gallery_dl/service.py` | 29–30, 37, 212, 222 | — | — | 201, 226 (4) | — |
| `app/services/instagram.py` | 65, 74–75, 156, 232, 257, 270, 340, 344, 697, 711, 756, 766 | — | — | 606–607, 782–783 (2)<br>311–312, 467, 498, 630, 639–640, 644, 663, 669, 700–701, 719, 758–759, 769 (4) | 53, 313, 468, 499, 608, 641, 702, 760, 784 |
| `app/services/media/delivery.py` | — | — | — | — | 329, 341, 410, 449, 456, 582, 736, 748, 756 |
| `app/services/media/materialization_race.py` | — | — | — | — | 39, 55 |
| `app/services/media/models.py` | — | — | — | 304, 307 (3) | — |
| `app/services/media/pipeline.py` | — | — | — | 195–196, 572–573, 793, 797 (2)<br>983–985, 1283, 1763–1765 (3)<br>662–664 (4)<br>1995 (7) | 226, 1010, 1069 |
| `app/services/media/providers/fxtwitter.py` | — | — | — | 228 (3) | — |
| `app/services/media/providers/http.py` | — | — | — | 299–301 (4) | — |
| `app/services/media/providers/ytdlp.py` | — | — | — | 159–160, 168, 226, 362 (3)<br>56–57, 63 (4) | — |
| `app/services/media/proxies.py` | — | — | — | 68, 129–130 (4) | — |
| `app/services/media/race.py` | — | — | — | 71, 236–237 (4) | 70 |
| `app/services/media/singleflight.py` | — | 123–124 | — | 110–111 (2) | — |
| `app/services/media/transport.py` | — | — | — | 186, 193–194, 810, 838–839, 967, 1168–1170 (2)<br>1390–1391, 1397–1398, 1685, 1688–1689, 1696–1697, 1713–1714 (3) | 809 |
| `app/services/orchestrator.py` | 151, 196, 208, 235–237, 260–262, 327, 409, 526, 530, 560, 563, 583, 592, 632, 650, 657, 694 | 35, 532, 540, 553, 562, 565, 656 | 573 (E1) | 398–399 (1)<br>56–57, 596–597, 661–662 (2)<br>541 (3)<br>105, 119–120, 253–254, 413–416, 443–446 (4)<br>38–39, 144–145, 147, 215–216, 531, 554, 561, 564, 566–567, 590 (5)<br>315–318 (6) | 58, 121, 217, 255, 400, 598, 663 |
| `app/services/pinterest.py` | 42, 181, 187, 213, 256, 317, 435 | — | — | 198, 298, 309–310, 407–408, 442 (4) | 409 |
| `app/services/tikwm.py` | 36, 38, 73, 75–77, 191, 207, 225, 229, 277, 288, 292, 295, 303, 356, 410, 412, 475 | — | 79–81, 98–100, 241–242, 278, 284, 310–312 (E4) | 324, 349–350, 424 (2)<br>109–110, 117–118, 157–158, 211–212, 228, 291, 386–387, 455–456, 488–489 (4) | 45, 119, 159, 354, 391, 460, 490 |
| `app/services/ytdlp/builders.py` | 91 | 6–7, 194–195 | — | 103–104 (3)<br>55, 58, 193 (4)<br>152, 168–169 (5) | — |
| `app/services/ytdlp/cookies.py` | 22, 33, 40, 43, 45, 48, 57, 115, 118, 128, 133, 140, 150 | — | — | 44 (6) | — |
| `app/services/ytdlp/exceptions.py` | 32, 39, 46, 53, 68, 77 | — | — | 57–59 (4) | — |
| `app/services/ytdlp/parsers.py` | 49, 264 | — | — | 17, 119, 132, 180, 183, 195, 205–207, 261 (3) | — |
| `app/services/ytdlp/service.py` | 63, 137 | — | — | 257–258 (2)<br>98, 117, 149, 211, 262–263, 299, 324 (4) | 143, 259, 264 |
| `app/tasks/janitor.py` | 44, 59, 180, 209, 229, 247, 259 | 190 | — | 199–200, 288–289 (6) | 201, 290 |

## Directive suffixes removed

- `app/api/routes.py` at 101, 161.
- `app/bot/callbacks.py` at 553, 567, 575, 586, 618, 910.
- `app/core/drain.py` at 296.
- `app/core/job_store.py` at 605, 830.
- `app/services/cobalt.py` at 38.
- `app/services/media/delivery.py` at 329, 341, 410, 449, 456, 582, 736, 748, 756.
- `app/services/media/materialization_race.py` at 39, 55.
- `app/services/media/pipeline.py` at 226, 1010, 1069.
- `app/services/media/race.py` at 70.
- `app/services/media/transport.py` at 809.
- `app/services/tikwm.py` at 45.
