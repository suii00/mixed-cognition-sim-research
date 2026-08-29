# Public disaster v3 execution incident: 2026-08-29

## Classification

この記録はsimulation結果の解釈ではなく、formal launcherの最初の実行で観測したoperational
incidentである。model output本文は原因調査に使用していない。

## Direct observations

- source Git SHA: `8bb2f81ca6c970c3fdf59df1f6b838fde8b082fc`
- batch ID: `public-disaster-matrix-20260829T000612695135Z-62cc4c89f924`
- public treeへpromoteしたrun: 0
- completed run: `public-disaster-v3-llama_only-free_text-s3101-wb`
  - status completed、60/60 steps
  - 2,880 logical calls、2,880 HTTP attempts
  - retry、transport、syntax、schema failureはすべて0
- cleanupでabortedになったrun: `public-disaster-v3-qwen_only-free_text-s3101-wa`
  - status aborted、54/60 completed steps
  - 2,640 logical calls、2,640 HTTP attempts
  - retry、transport、syntax、schema failureはすべて0
- attempt全体: 5,520 logical calls、5,520 HTTP attempts
- worker failure codes: `strict_validation_failed`、`managed_worker_error`
- staging publication scan: finding 0
- cleanup後: selected GPU memoryは全てbaseline、formal listenerは全てclosed

## Mechanical diagnosis

completed runへ`tools/validate_run.py --strict`を適用した結果、利用可能なstrict checksはPASSし、
exit codeも0だった。同時に、dependency completeness、global event identity、external signature、
意図的に除外したruntime bindingなど、現行artifactだけでは証明できない事項が
`UNVERIFIABLE`として報告された。

worker gateは`report.valid`だけでなく`report.unverifiable`が空であることまで要求していたため、
validなcompleted runを誤ってfailureに分類した。これはsimulation、model response、schema、
publication scanのfailureではなく、launcher validation gateの実装不一致である。

## Corrective action

- strict gateは`report.valid == true`を必須条件とする。
- unverifiable事項は隠さず、runごとの件数とcanonical digestをaggregate evidenceへ保存する。
- validation error、publication finding、runtime-binding残存、retry/failure counterは従来どおり
  一件でもあればfail closedとする。
- このattemptはformal 60-run結果へ混ぜず、追加GPU実行は新しいcall envelopeの承認後だけ行う。
