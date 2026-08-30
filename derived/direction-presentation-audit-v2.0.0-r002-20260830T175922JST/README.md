# Direction-presentation audit v2 r002

## 結論

この single-seed engineering audit では、左右の提示順だけでは通信下の右収束を反転できなかった。通信あり6セルはすべて右cascade、左→右提示の隔離3セルも右cascadeとなり、右→左提示の隔離3セルだけはcascade条件を満たさなかった。

事前登録decision rulesでは、提示順感度は通信ありで不成立、隔離で3/3 rotations成立した。通信感度は左→右提示で2/3、右→左提示で3/3 rotations成立し、提示順×通信の相互作用は3/3 rotationsで成立した。一方、全セルの総actionに占めるright率が0.75以上という robust-right rule は不成立だった。

この結果は「単純なenum順だけが右収束を作った」という説明とは整合しにくいが、語彙的な左右バイアス、世界状態、自己記憶、受信内容の再利用、または人口レベルのモデル特性を単独で同定するものではない。

## 直接観測

- 12/12 runがcompleted、各10 steps・24 agents・480 logical calls。
- 合計5,760 attempts、Phase 1 rows 2,880、action rows 2,880。
- non-empty authored message rows 2,494、delivered message rows 1,402。
- parse/schema/syntax/transport failuresおよびgeneration retriesはすべて0。
- 12/12 runで `git_dirty=false`、source SHAは `9486ec0cb11d758aa2bd53c04541699ed7f0f0ac`。
- 横移動だけを分母としたright shareは全12セルで0.889–1.000。
- 全actionを分母としたright率は0.554–0.992。右→左提示・隔離ではupが78–87/240件あり、right率が0.554–0.588まで下がったが、horizontal right shareは0.899–0.993だった。
- communicationあり6セルはすべてright cascade。左→右提示・隔離3セルもright cascade。右→左提示・隔離3セルはcascadeなし。

## 機械導出

提示順差（LR minus RL overall right rate）は通信ありで `-0.0667, -0.0167, -0.0333`、隔離で `+0.2208, +0.2542, +0.2208` だった。提示順感度の閾値 `|difference| >= 0.1` は通信あり0/3、隔離3/3 rotationsで成立した。

通信差（communication minus isolation overall right rate）はLR提示で `+0.0542, +0.1667, +0.1292`、RL提示で `+0.3417, +0.4375, +0.3833` だった。通信感度はLR提示2/3、RL提示3/3 rotationsで成立した。

差の差は `-0.2875, -0.2708, -0.2542` で、相互作用閾値を3/3 rotationsで満たした。特に隔離条件ではQwenのLR minus RL right-rate差が `+0.5500, +0.3750, +0.4625` と大きく、提示順感度の主な機械的寄与だった。

## 解釈境界

- `research_eligible=false`。single seedのengineering diagnosticsであり、人口推論はしない。
- 配送はexposureであり、再利用・採用ではない。受信行だけから社会的採用を主張しない。
- `reasoning` はschema上の空文字であり、内部推論の証拠として扱わない。
- model-to-agent rotationとagent-step rowsを独立replicateとして数えない。
- strict validatorで利用可能なchecksは全件合格したが、dependency完全性、全primary rowのglobal event identity、外部署名、運用endpoint identityなど、schema上のunverifiable項目は残る。

## Provenance

- Protocol: `engineering-direction-presentation-communication-audit-v2.0.0`
- Metric: `direction-presentation-communication-audit-metric-v1.0.0`
- Source: `9486ec0cb11d758aa2bd53c04541699ed7f0f0ac`
- Result SHA-256: `c10a9853079ad2747bcdc00ef3760692748e7efe7d2c1f72766c80e5d440dda9`
- Frozen plan: `inputs/plan.json`
- Frozen manifest: `inputs/manifest.json`
- Machine-readable result: `result.json`
- Run/evidence hashes: `retrieval_manifest.json`

## r001の扱い

r001は12セルとも技術的には完走したが、同一source checkout内に先行runの未追跡生成物が残り、2–12セル目が `git_dirty=true` を記録した。完全metricは意図どおり拒否した。r001 rawはforensic copyに保持し、r002のprimary解析には混ぜていない。
