# Test comment audit

Original line numbers refer to `17ac60c7171a` under `tests/`. Counts are Python
comment tokens. Every line in a multiline comment counts once. Fixture payloads
and docstrings remain part of the test data.

| Classification | Standalone | Inline | Total |
| --- | ---: | ---: | ---: |
| Tautology | 168 | 54 | 222 |
| Stale | 18 | 1 | 19 |
| Excuse | 1 | 0 | 1 |
| Decision Record | 61 | 2 | 63 |
| Directive retained | 5 | 47 | 52 |
| Total | 253 | 104 | 357 |

305 comment tokens were removed. Three retained directives had their
explanatory prose moved to decision 0011.

- 0010 records how the tests bound fixtures and time behavior. [Test watchdogs](../adr/0010-tests-watchdogs.md)
- 0011 records boundaries kept real in offline tests. [Offline fixtures](../adr/0011-tests-fixture-boundaries.md)
- 0012 records durable behavior exercised through lifecycle and callback tests. [Durable recovery tests](../adr/0012-tests-durable-recovery.md)
- 0013 records fallback and clip behavior verified by media tests. [Media selection tests](../adr/0013-tests-media-selection.md)
- The cookie exporter test works around stream changes on import. [Issue #146](https://github.com/Le-Who/ytdlbot/issues/146)

| File | Tautology | Stale | Excuse | Decision Record | Retained directive |
| --- | --- | --- | --- | --- | --- |
| `acceptance/test_docker_evidence_adapter.py` | — | — | — | — | 349 (rationale in 0011) |
| `acceptance/test_media_release.py` | — | — | — | 0010 (797–798) | — |
| `conftest.py` | 11, 16, 25 | 34 | — | — | — |
| `core/test_download_queue_cancellation.py` | — | — | — | — | 96 |
| `core/test_drain.py` | 679 | — | — | 0010 (534–535)<br>0011 (81–82) | — |
| `core/test_job_store.py` | 360 | — | — | — | — |
| `core/test_posix_process_cleanup.py` | — | — | — | 0011 (43) | — |
| `core/test_process_supervisor.py` | — | — | — | 0010 (426, 453) | 454 |
| `deploy/test_ci_environment.py` | — | — | — | 0011 (73, 118, 168–169) | — |
| `deploy/test_release_environment.py` | — | — | — | 0011 (42–43) | — |
| `deploy/test_release_scripts.py` | — | — | — | 0010 (244–245)<br>0011 (99–100) | — |
| `media/providers/test_http_providers.py` | — | — | — | — | 1260, 1275, 1295 |
| `media/providers/test_ytdlp_provider.py` | — | — | — | 0011 (695, 713) | — |
| `media/test_delivery.py` | — | — | — | — | 117, 267, 302, 336, 376 |
| `media/test_models.py` | — | — | — | — | 194 |
| `media/test_pipeline.py` | — | — | — | 0011 (1499) | — |
| `media/test_singleflight.py` | — | — | — | — | 338, 345 |
| `media/test_transport.py` | 439 | — | — | 0010 (832)<br>0013 (1097) | 145, 422–426, 475 |
| `test_bot_boundary_contracts.py` | — | — | — | 0012 (270) | — |
| `test_bot_callbacks.py` | 34, 52, 57, 62, 101, 284, 359, 437, 512 | 23–24, 294 | — | 0011 (42, 297, 300) | 1 |
| `test_bot_keyboards.py` | 28, 39, 56, 72, 84, 87, 92–93 | 4 | — | — | — |
| `test_bot_messages.py` | 42, 113, 132, 137 | — | — | 0012 (398) | — |
| `test_cache.py` | 17, 75 | — | — | — | — |
| `test_callback_overlap.py` | — | — | — | 0012 (108) | — |
| `test_callback_resource_regressions.py` | — | — | — | — | 206 (rationale in 0011) |
| `test_callbacks_exceptions.py` | 73, 79, 87, 94, 99, 105, 124 | — | — | — | 1 |
| `test_cobalt_service.py` | — | — | — | — | 6 |
| `test_converter.py` | 16, 21, 27, 43, 77, 87, 123, 167, 206, 208, 223 | — | — | — | 249 |
| `test_cookies_manager.py` | 31, 36, 41, 55, 59, 71, 75, 79 | 9, 82 | — | — | — |
| `test_core_utils.py` | 26, 30, 40, 77, 90 | — | — | — | 34 |
| `test_core_utils_extract.py` | 7 | — | — | — | — |
| `test_direct_highlight_chain.py` | — | — | — | — | 9 (rationale in 0011) |
| `test_downloader_service.py` | 41 | — | — | 0010 (143–144) | — |
| `test_endpoints.py` | — | 60 | — | — | — |
| `test_export_cookies.py` | 76 | — | 26 | — | — |
| `test_fast_path_opts.py` | 39, 85, 386–387, 395 | — | — | — | 1 |
| `test_fixes_verification.py` | 19, 52 | 4–5, 11 | — | 0013 (27) | — |
| `test_gallery_dl_service.py` | 36, 124, 129, 133, 149, 236, 248 | — | — | — | — |
| `test_gif_callback_chain.py` | — | — | — | — | 12 |
| `test_global_error_handler.py` | 5, 14, 23, 30, 36, 45, 47, 50, 62, 65, 68 | — | — | — | — |
| `test_group_and_converter_extended.py` | 202, 220, 248 | 206–207 | — | — | — |
| `test_group_logic.py` | 65, 188 | — | — | — | — |
| `test_health_readiness.py` | — | — | — | — | 44, 96, 111, 127 |
| `test_ig_callback_chains.py` | — | — | — | 0011 (158, 225) | — |
| `test_instagram_service.py` | 172 | — | — | 0011 (404) | 9, 406 |
| `test_integration.py` | 60, 112, 115, 322, 338 | — | — | — | 1 |
| `test_integration_real.py` | 98, 102, 131 | 27–28 | — | — | — |
| `test_janitor_service.py` | 45, 91 | — | — | — | — |
| `test_limiter.py` | 23, 29, 31–32, 39–41, 43, 45–47, 65–67, 83, 86, 89, 114 | — | — | — | — |
| `test_main_utils.py` | 13, 17, 22, 26, 31, 35 | 7 | — | — | — |
| `test_media_retry.py` | 201 | — | — | 0012 (589, 702) | — |
| `test_messages_errors.py` | 39, 175, 244 | — | — | — | — |
| `test_metrics.py` | — | — | — | — | 275, 281–282, 315–316, 381, 465, 512, 556 |
| `test_new_features.py` | 11, 99, 101, 116, 188 | — | — | — | — |
| `test_parsers_filesize.py` | 34, 37 | — | — | — | — |
| `test_parsers_full.py` | 21, 35, 52, 72, 89, 100, 111, 177, 210, 241, 258, 290, 305 | — | — | — | — |
| `test_pinterest_service.py` | 258 | — | — | 0011 (223) | 9, 224 |
| `test_pipeline_entrypoints.py` | — | — | — | 0010 (310–311) | — |
| `test_professional_features.py` | 15 | 6 | — | — | — |
| `test_property_based.py` | 18, 44, 95, 125, 151 | 122 | — | — | — |
| `test_runtime_configuration.py` | — | — | — | 0012 (24–25, 87–88) | — |
| `test_sender_service.py` | — | — | — | 0011 (100) | 101 |
| `test_slideshow_callbacks.py` | 39, 176, 209 | — | — | — | 1 |
| `test_storage_memory.py` | 37, 56, 65, 68, 71, 74 | — | — | — | — |
| `test_storage_redis.py` | 24, 65, 75, 83, 111, 146 | — | — | 0012 (164) | — |
| `test_tiktok_content_classification.py` | 9, 13, 16, 19, 23, 28, 30 | — | — | — | — |
| `test_tikwm_optimized.py` | 96 | — | — | 0011 (81) | 82 |
| `test_tikwm_service.py` | — | — | — | — | 7 |
| `test_video_meta.py` | 42, 49 | — | — | 0013 (275) | — |
| `test_webhook_durability.py` | 421–422 | — | — | 0011 (571–572)<br>0012 (251, 360–361, 404–405, 425–426, 666–667, 724, 929, 932) | — |
| `test_ytdlp_exceptions.py` | 47, 55 | — | — | — | — |
| `test_ytdlp_parsers.py` | 20, 25, 29, 34, 39, 50, 63, 97, 184, 193, 206, 238, 272, 345–346 | — | — | — | — |
| `test_ytdlp_service.py` | 105, 119–120, 137, 152, 165, 172 | — | — | — | — |
| `test_ytdlp_service_list_formats.py` | 17, 98, 208, 236, 242 | 15 | — | — | — |
| `test_ytdlp_url_helpers.py` | 11, 16, 19, 25, 29, 33, 36 | — | — | — | — |
