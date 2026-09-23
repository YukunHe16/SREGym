# Crossover startup and pre-agent exclusion

The first frozen schedule was started in batch `results/jev-crossover-20260923T020640.753649Z`. Run 1 deployed and injected `wrong_dns_policy_astronomy_shop`, then the Jev HTTP request received a 403 before any Codex agent started. The official result row has `routing_failed=True`, `incomplete_reason=model_routing_failed`, and no diagnosis or mitigation verdict; there is no `codex_results_*.json` agent artifact. Cleanup finished. This attempt is excluded from model outcomes and preserved unchanged.

A separate diagnostic POST of the saved, redacted route request reproduced HTTP 403 with Cloudflare error 1010 using Python's default urllib client header. The same request, with `User-Agent: SREGym-Jev-Router/1.0`, returned HTTP 200. The diagnostic response body was discarded; its choice was not used for an agent run. The TypeSafe key was entered with no echo and was not saved.

The scoped compatibility fix adds that explicit client identifier to the routing request and records safe numeric HTTP status on any future failure. It does not change the payload, selection criteria, candidate models, agent prompt, judge, or scoring. A new frozen batch must start from run 1 after this source change; no completed model run is being repeated.
