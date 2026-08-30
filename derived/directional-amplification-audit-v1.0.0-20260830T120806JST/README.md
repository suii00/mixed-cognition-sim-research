# 方向増幅 engineering audit v1 実行結果

実行日: 2026-08-30  
source commit: `ae7bba426a27fccabf0df2e332507cb8383c10ca`  
protocol: `engineering-directional-amplification-audit-v1.0.0`  
metric: `directional-amplification-audit-metric-v1.0.0`  
資格: `research_eligible=false`

## 結論

固定した6セルはすべて実行されたが、`c23-r0` がstep 8のPhase 3でJSONを閉じる前に
256 completion tokensへ到達し、syntax failureでabortした。このためmatrix全体は不合格であり、
完走必須の事前登録metricは結果を生成していない。完走5セルだけを用いる診断結果は
`incomplete_result.json` に分離した。

比較可能な `r1` と `r2` では、履歴を3件から23件へ広げると高IDブロックの可視slot占有率は
大きく低下したが、非高ID agentの1-step-lag整列率と全体の右行動率はどちらも上昇した。
したがって、観測できた2ペアは「高ID送信者だけが見える切り詰めが右同調を作る」という
単純な説明を支持しない。

## Direct observations

| cell | 高IDブロック | status | 右行動率 | lag-1整列率 | 高ID可視slot比率 |
| --- | --- | --- | ---: | ---: | ---: |
| `c03-r0` | ELYZA | completed | 0.850 | 0.792 | 1.000 |
| `c03-r1` | Qwen | completed | 0.717 | 0.732 | 0.832 |
| `c03-r2` | Swallow | completed | 0.775 | 0.785 | 0.743 |
| `c23-r0` | ELYZA | aborted at step 8 Phase 3 | — | — | — |
| `c23-r1` | Qwen | completed | 0.858 | 0.861 | 0.315 |
| `c23-r2` | Swallow | completed | 0.917 | 0.972 | 0.315 |

- 完走: 5/6 run
- logical LLM calls / HTTP attempts: 2,784 / 2,784
- transport failure: 0
- syntax parse attempt failure / terminal syntax failure: 1 / 1
- schema validation failure: 0
- 完走5 runはstrict validation合格
- 全6 runのpublication findingは0
- source checkoutは実行前後ともclean
- cleanup後に全process group停止、選択GPUはbaselineへ復帰

abortしたattemptはELYZAのagent 18、step 8 Phase 3だった。HTTP transportは成功し、
prompt 998 tokens、completion 256 tokens、`finish_reason=length` だった。raw textはJSON objectで
始まったが閉じるobject文字で終わらず、schema validationへ到達しなかった。本文の意味内容は
診断に使用していない。

## Mechanical derivations

完走したpaired cellsの `c03 - c23` 差は次の通り。

| rotation | 高IDブロック | 高ID可視slot比率差 | lag-1整列率差 | 右行動率差 |
| --- | --- | ---: | ---: | ---: |
| `r1` | Qwen | +0.517 | -0.129 | -0.142 |
| `r2` | Swallow | +0.428 | -0.188 | -0.142 |

符号は `c03 - c23` なので、整列率と右行動率の負値は全履歴条件 `c23` の方が高いことを示す。
`c03` の高ID可視slot優位は機械的に確認できた。一方、行動差は事前予測と逆方向だった。

可視メッセージslot中の文字「右」のみを含むslot数は、`c23-r1` で4,164/5,037、
`c23-r2` で4,620/5,106だった。これは文字列の直接計数であり、受信者による再利用、採用、
信念変化を意味しない。

## Pre-registered engineering decisions

- matrix acceptance: **failed** — 事前登録セル1件がabort
- mechanical sender-order dominance: **indeterminate** — `r1` と `r2` は閾値を満たしたが、`r0` pairが欠損
- behavioral context signal: **failed** — 完走2 pairはいずれも予測と逆で、残る1 pairだけでは必要な2 rotationへ到達不能
- context-robust right pattern: **failed** — `c03-r1` の右行動率0.717が事前閾値0.75未満

## Interpretation

現時点で直接示せるのは、送信者順と3件切り詰めが可視情報を高ID側へ偏らせること、そして
その偏りを除いた観測可能な2ペアで右行動と整列が弱まらなかったことである。後者は、右方向の
パターンが高IDブロックだけから生じるのではなく、複数モデルの初期傾向と広いメッセージ露出が
組み合わさっている可能性を示す。ただし単一seed、各セル1 run、不完全matrixなので仮説生成の
範囲を超えない。

次のprospective protocolでは、まず出力打切りを防ぐ版付きresponse/prompt contractを全セルへ
一律適用する。その上で、左右選択肢の提示順を鏡像化したcontrol、通信なしcontrol、複数seedを
組み合わせる。今回のrunを修復版へ混入、上書き、または正式研究runへ再分類してはならない。

## Artifact locations

- 固定入力: `inputs/directional_amplification_audit_v1/`
- launcher検証証跡: `validation/`
- 不完全matrix診断: `incomplete_result.json`
- 回収・hash記録: `retrieval_manifest.json`
- 完走raw runs: repository `runs/output_<run_id>`
- abort raw run: repositoryでignoreされたbyte-identical forensic copy。正確な相対pathとtree hashは
  `retrieval_manifest.json` に記録した。
