# Public disaster v3.2 token-limit incident: 2026-08-29

## Classification

これはsimulation結果の解釈ではなく、source
`97171c51ea446a61bec920bd6ec01a2df670935e`で観測したoperational incidentである。
model output本文は表示、実行、または変更判断の意味内容として利用していない。

## Direct observations

- batch ID: `public-disaster-matrix-20260829T081159865648Z-69d49ea7c3b0`
- public treeへpromoteしたrun: 0
- worker-verified completed runs: 12
- aborted runs: 2
- 未開始runs: 46
- logical calls / HTTP attempts: 29,832 / 29,832
- generation retry: 0
- transport failure: 0
- syntax parse attempt failure / terminal syntax failure: 1 / 1
- schema validation failure: 0
- Llama terminal attempt:
  - run ID: `public-disaster-v3p2-llama_only-free_text-s3102-wa`
  - step 12、Phase 3、agent 22
  - HTTP status 200、transport `ok`、`finish_reason=length`
  - prompt 637 tokens、completion 1,024 tokens、total 1,661 tokens
  - HTTP response 3,922 bytes、SHA-256
    `6752f47c00dbdbc0f0a30945bca018c052bc277dca0c130ef9731dfaaa51ca6e`
  - raw output length 3,236 characters
  - object開始文字あり、object終了文字なし
  - `LLMSyntaxError`、parse `invalid`、schema validation未到達
  - generation attempt / HTTP attempt: 1 / 1
- Qwen cleanup-aborted run:
  - run ID: `public-disaster-v3p2-qwen_only-free_text-s3102-wb`
  - 9/60 completed steps、456 calls / attempts
  - step 10 `step_start`で`KeyboardInterrupt`
  - retry、transport、syntax、schema failure 0
- staging publication scan: unapproved match 0
- runtime binding string: 0
- repository `.log` file: 0
- cleanup後: managed vLLM/worker process 0、formal listener 0、全GPU 1 MiB baseline
- tracked public tree: clean
- v3.0.0--v3.2.0の全operational attempt累積: 163,512 calls / HTTP attempts

## Mechanical diagnosis and disposition

HTTP transportは成功したが、Llama generationが1,024-token ceilingでJSON objectを閉じる前に停止し、
strict parserがterminal syntax failureとして正しくrunをabortした。もう一方のworkerはmatrix全体の
fail-closed cleanupで停止した。completed 12 runとaborted raw attemptをformal resultへ集計せず、
ignored stagingを監査用に変更せず保持する。

1,024 tokensへの拡張だけではresponse contractを全attemptで完結させる十分条件ではなかった。
追加のprobe、response contract変更、またはtoken/context条件変更は、このattemptの再開として扱わず、
新しいprospective protocol、source SHA、call/time envelope、明示承認を要求する。
