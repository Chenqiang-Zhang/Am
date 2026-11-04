"""README §8's result tables, in Japanese or Chinese, from the result files of eval/static_eval.py.
The output depends on nothing but the files, so the same files give the same bytes."""

from typing import Any, Mapping, Sequence

LANGUAGES = ("ja", "zh")
GROUPS = ("all", "1-3", "1", "2-3", "4-10", "11+")
TEXT: dict[str, dict[str, str]] = {
    "ja": {
        "tuning": "調整（検証用のユーザ {users} 人、{evaluations} 回）",
        "config": "設定",
        "scheme": "シード",
        "weights": "λ（共プレイ / タグ / 開発元・販売元 / 人気）",
        "normalization": "開発元・販売元の割り方",
        "vs_reference": "graph_v1_default との差（NDCG@10）",
        "chosen": "選んだ設定",
        "best": "目的関数が最大の設定",
        "reference": "graph_v1_default",
        "seed_weight": "Σ_s w_s",
        "max": "最大値",
        "log_hours": "ln(1 + プレイ時間)",
        "uniform": "1",
        "top": "上位 {n} 本",
        "all_seeds": "全部",
        "tuning_note": (
            "推定値は事後層別、CI は配対ブートストラップ（B = {B:,}）。最大の設定との差が 1 SE 以内の設定は {qualifying} 個。"
        ),
        "c1": "C1（実装の確認、検証用のユーザ {evaluations} 回）",
        "check": "確認",
        "baseline": "比べる手法",
        "agreeing": "一致（順位が同じか、両方 50 位より下）",
        "result": "判定",
        "overall": "全体（テスト用のユーザ {users} 人、{evaluations} 回、事後層別、[95% CI]）",
        "method": "手法",
        "strata": "層ごとの NDCG@10（可視のゲーム数、人数）",
        "contrasts": "差（NDCG@10、[95% CI]）",
        "contrast": "比較",
        "claims": "判定",
        "claim": "内容",
        "value": "値",
        "C1": "peer_only（uniform_all）と item_knn、pop_only と popularity の一致率が {threshold} 以上",
        "C2": "peer_only − item_knn の 95% CI が [−{margin}, +{margin}] の内側（同等）",
        "C3": "graph_tuned − peer_only > 0（{group}、Holm、97.5% CI）",
        "C4": "{contrast} の 95% CI の下限 > {bound}",
        "C5": "現行の推薦の根拠が、推薦ごとに全部成り立つ割合 = 1.0（v0 は参考）",
        "pass": "成立",
        "fail": "不成立",
        "pending": "未確定",
        "error": "使えない",
        "post_stratified": "事後層別",
        "v0": "v0 との比較（v0 用のユーザ {users} 人、rep 0、[95% CI]）",
        "descriptive": "隠したゲームで分けた結果（記述的、NDCG@10、[95% CI]）",
        "hidden_under_1h": "1 時間未満",
        "hidden_1h_or_more": "1 時間以上",
        "hidden_valve": "Valve の作品",
        "hidden_not_valve": "Valve 以外",
        "share": "回数の割合",
        "beyond": "推薦の偏り（{users} 人、rep 0、上位 {k} 件）",
        "coverage": "カバレッジ@{k}（÷ {denominator}）",
        "distinct": "現れたゲーム",
        "never_played": "誰も遊んでいないゲーム",
        "diversity": "多様性@{k}",
        "novelty": "新規性@{k}",
        "head": "人気上位 {n} 本の割合",
        "all": "全体",
    },
    "zh": {
        "tuning": "调参（验证用户 {users} 人、{evaluations} 次）",
        "config": "配置",
        "scheme": "种子",
        "weights": "λ（共玩 / 标签 / 开发商・发行商 / 热门度）",
        "normalization": "开发商・发行商的除数",
        "vs_reference": "与 graph_v1_default 的差（NDCG@10）",
        "chosen": "选定的配置",
        "best": "目标函数最大的配置",
        "reference": "graph_v1_default",
        "seed_weight": "Σ_s w_s",
        "max": "最大值",
        "log_hours": "ln(1 + 游玩时长)",
        "uniform": "1",
        "top": "前 {n} 款",
        "all_seeds": "全部",
        "tuning_note": (
            "估计值为事后分层，CI 为配对 bootstrap（B = {B:,}）。与最大配置之差在 1 个 SE 以内的配置共 {qualifying} 个。"
        ),
        "c1": "C1（实现检查，验证用户 {evaluations} 次）",
        "check": "检查",
        "baseline": "对照的方法",
        "agreeing": "一致（名次相同，或都在第 50 名以后）",
        "result": "判定",
        "overall": "全体（测试用户 {users} 人、{evaluations} 次，事后分层，[95% CI]）",
        "method": "方法",
        "strata": "各层的 NDCG@10（可见游戏数、人数）",
        "contrasts": "差（NDCG@10，[95% CI]）",
        "contrast": "比较",
        "claims": "判定",
        "claim": "内容",
        "value": "值",
        "C1": "peer_only（uniform_all）与 item_knn、pop_only 与 popularity 的一致率在 {threshold} 以上",
        "C2": "peer_only − item_knn 的 95% CI 落在 [−{margin}, +{margin}] 内（等价）",
        "C3": "graph_tuned − peer_only > 0（{group}，Holm，97.5% CI）",
        "C4": "{contrast} 的 95% CI 下限 > {bound}",
        "C5": "现行推荐的依据逐条全部成立的比例 = 1.0（v0 仅供参考）",
        "pass": "成立",
        "fail": "不成立",
        "pending": "未定",
        "error": "无法使用",
        "post_stratified": "事后分层",
        "v0": "与 v0 的比较（v0 用户 {users} 人，rep 0，[95% CI]）",
        "descriptive": "按被隐藏游戏划分的结果（描述性，NDCG@10，[95% CI]）",
        "hidden_under_1h": "不足 1 小时",
        "hidden_1h_or_more": "1 小时以上",
        "hidden_valve": "Valve 的作品",
        "hidden_not_valve": "Valve 以外",
        "share": "次数占比",
        "beyond": "推荐的偏向（{users} 人，rep 0，前 {k} 名）",
        "coverage": "覆盖率@{k}（÷ {denominator}）",
        "distinct": "出现的游戏",
        "never_played": "无人玩过的游戏",
        "diversity": "多样性@{k}",
        "novelty": "新颖性@{k}",
        "head": "热门前 {n} 款的占比",
        "all": "全体",
    },
}


def number(value: float | None) -> str:
    return "–" if value is None else f"{value:.4f}"


def signed(value: float) -> str:
    return f"{value:+.4f}"


def with_interval(row: Mapping[str, Any], value: str = "estimate", sign: bool = False) -> str:
    form = signed if sign or value == "delta" else number
    return f"{form(row[value])} [{form(row['lo'])}, {form(row['hi'])}]"


def contrast_name(name: str) -> str:
    return name.replace(" - ", " − ")


def table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    return "\n".join(lines + ["| " + " | ".join(row) + " |" for row in rows])


def keyed(rows: Sequence[Mapping[str, Any]], field: str) -> dict[tuple[Any, ...], Mapping[str, Any]]:
    return {(row[field], row["metric"], row["k"], row["group"]): row for row in rows}


class Report:
    def __init__(self, lang: str) -> None:
        self.text = TEXT[lang]
        self.parts: list[str] = []

    def t(self, key: str, **values: Any) -> str:
        return self.text[key].format(**values)

    def group(self, name: str) -> str:
        return self.t("all") if name == "all" else name

    def section(self, title: str, *blocks: str) -> None:
        self.parts.append("\n\n".join([f"#### {title}", *blocks]))

    def scheme(self, config: Mapping[str, Any], schemes: Mapping[str, Mapping[str, Any]]) -> str:
        scheme = schemes[config["scheme"]]
        limit = scheme["peer_limit"]
        seeds = self.t("all_seeds") if limit is None else self.t("top", n=limit)
        return f"{self.t(scheme['weighting'])}、{seeds}"


def render(lang: str, tuning: Mapping[str, Any] | None, test: Mapping[str, Any] | None, c5: Mapping[str, Any]) -> str:
    report = Report(lang)
    if tuning is not None:
        tuning_tables(report, tuning)
    if test is not None:
        test_tables(report, test, c5)
    return "\n\n".join(report.parts) + "\n"


def tuning_tables(report: Report, tuning: Mapping[str, Any]) -> None:
    t = report.t
    rows = {row["id"]: row for row in tuning["configs"]}
    chosen = (("chosen", tuning["chosen"]["id"]), ("best", tuning["best"]["id"]), ("reference", tuning["reference"]))
    lines = []
    for label, config_id in chosen:
        row = rows[config_id]
        lines.append(
            [
                t(label),
                report.scheme(row, tuning["schemes"]),
                " / ".join(f"{row['weights'][path]:g}" for path in ("peer", "tag", "studio", "popular")),
                t(row["studio_normalization"]),
                number(row["NDCG@10"]),
                number(row["HR@10"]),
                with_interval(row["vs_reference"], "delta"),
            ]
        )
    header = [t("config"), t("scheme"), t("weights"), t("normalization"), "NDCG@10", "HR@10", t("vs_reference")]
    qualifying = sum(row["qualifies"] for row in tuning["configs"])
    note = t("tuning_note", B=tuning["bootstrap"]["B"], qualifying=qualifying)
    title = t("tuning", users=f"{sum(tuning['users'].values()):,}", evaluations=f"{tuning['evaluations']:,}")
    report.section(title, table(header, lines), note)
    c1 = tuning["C1"]
    checks = [
        [name, check["baseline"], f"{check['agreeing']:,} / {check['evaluations']:,}", f"{check['agreement']:.2%}"]
        for name, check in c1["checks"].items()
    ]
    verdict = f"{t('result')}: {t(c1['status'])}"
    title = t("c1", evaluations=f"{tuning['evaluations']:,}")
    report.section(title, table([t("check"), t("baseline"), t("agreeing"), "%"], checks), verdict)


def test_tables(report: Report, test: Mapping[str, Any], c5: Mapping[str, Any]) -> None:
    t = report.t
    estimates, contrasts = keyed(test["estimates"], "method"), keyed(test["contrasts"], "contrast")
    methods = list(dict.fromkeys(row["method"] for row in test["estimates"]))
    users = sum(test["users"].values())
    columns = [(metric, k) for k in (10, 20, 50) for metric in ("NDCG", "HR")]

    def overall(method: str, metric: str, k: int) -> str:
        row = estimates[(method, metric, k, "all")]
        return with_interval(row) if k == 10 else number(row["estimate"])

    rows = [[method] + [overall(method, metric, k) for metric, k in columns] for method in methods]
    header = [t("method")] + [f"{metric}@{k}" for metric, k in columns]
    report.section(t("overall", users=f"{users:,}", evaluations=f"{test['evaluations']:,}"), table(header, rows))

    strata = ("1", "2-3", "4-10", "11+", "1-3", "all")
    sizes = dict(test["users"], **{"1-3": test["users"]["1"] + test["users"]["2-3"], "all": users})
    header = [t("method")] + [f"{report.group(name)} ({sizes[name]:,})" for name in strata]
    rows = [
        [method] + [number(estimates[(method, "NDCG", 10, name)]["estimate"]) for name in strata] for method in methods
    ]
    report.section(t("strata"), table(header, rows))
    report.section(t("contrasts"), contrast_table(report, contrasts, [row["contrast"] for row in test["contrasts"]]))
    claims_table(report, test["claims"], c5)

    v0 = test["v0_subset"]
    v0_estimates = keyed(v0["estimates"], "method")
    rows = [
        [method] + [with_interval(v0_estimates[(method, metric, 10, "all")]) for metric in ("NDCG", "HR")]
        for method in dict.fromkeys(row["method"] for row in v0["estimates"])
    ]
    names = [row["contrast"] for row in v0["contrasts"]]
    v0_contrasts = contrast_table(report, keyed(v0["contrasts"], "contrast"), names)
    title = t("v0", users=f"{sum(v0['users'].values()):,}")
    report.section(title, table([t("method"), "NDCG@10", "HR@10"], rows), v0_contrasts)
    descriptive_table(report, test["descriptive"])
    beyond_table(report, test["beyond_accuracy"])


def contrast_table(report: Report, contrasts: Mapping[tuple[Any, ...], Mapping[str, Any]], names: Sequence[str]) -> str:
    header = [report.t("contrast")] + [report.group(group) for group in GROUPS]
    rows = [
        [contrast_name(name)] + [with_interval(contrasts[(name, "NDCG", 10, group)], "delta") for group in GROUPS]
        for name in dict.fromkeys(names)
    ]
    return table(header, rows)


def claims_table(report: Report, claims: Mapping[str, Any], c5: Mapping[str, Any]) -> None:
    t = report.t
    c1, c2, c3, c4 = claims["C1"], claims["C2"], claims["C3"], claims["C4"]
    agreements = ", ".join(f"{check['agreement']:.2%}" for check in c1["checks"].values())
    rows = [
        ["C1", t("C1", threshold=f"{c1['threshold']:.1%}"), agreements, t(c1["status"])],
        ["C2", t("C2", margin=c2["margin"]), with_interval(c2, "delta"), t(c2["status"])],
    ]
    for role, test in c3["tests"].items():
        value = f"{with_interval(test['ci_975'], 'delta')}, p(Holm) = {test['p_holm']:.4f}"
        rows.append([f"C3 ({role})", t("C3", group=report.group(test["ci_975"]["group"])), value, t(test["status"])])
    for check in c4["checks"]:
        claim = t("C4", contrast=contrast_name(check["contrast"]), bound=f"{check['lower_bound_above']:+g}")
        rows.append(["C4", claim, with_interval(check, "delta"), t(check["status"])])
    value = c5.get("error", "–")
    if c5["status"] in ("pass", "fail"):
        low, high = c5["v0_ci"]
        value = f"{c5['current']:.4f}（v0 {c5['v0']:.4f} [{low:.4f}, {high:.4f}]"
        if c5.get("v0_post_stratified") is not None:
            low, high = c5["v0_post_stratified_ci"]
            value += f"; {t('post_stratified')} {c5['v0_post_stratified']:.4f} [{low:.4f}, {high:.4f}]"
        value += "）"
    rows.append(["C5", t("C5"), value, t(c5["status"])])
    report.section(t("claims"), table(["", t("claim"), t("value"), t("result")], rows))


def descriptive_table(report: Report, descriptive: Mapping[str, Any]) -> None:
    t = report.t
    groups = descriptive["groups"]
    header = [t("method")] + [
        f"{t(name)} ({group['evaluations']:,}, {group.get('share', 0.0):.1%})" for name, group in groups.items()
    ]
    filled = next(group for group in groups.values() if "estimates" in group)

    def cell(group: Mapping[str, Any], field: str, name: str, sign: bool) -> str:
        if field not in group:
            return "–"
        value = group[field][name]
        return with_interval(value["NDCG@10"] if field == "estimates" else value, sign=sign)

    rows = [
        [method] + [cell(group, "estimates", method, False) for group in groups.values()]
        for method in filled["estimates"]
    ]
    rows += [
        [contrast_name(contrast)] + [cell(group, "contrasts", contrast, True) for group in groups.values()]
        for contrast in filled["contrasts"]
    ]
    report.section(t("descriptive"), table(header, rows))


def beyond_table(report: Report, beyond: Mapping[str, Any]) -> None:
    t = report.t
    k = beyond["k"]
    header = [
        t("method"),
        t("coverage", k=k, denominator=f"{beyond['coverage_denominator']:,}"),
        t("distinct"),
        t("never_played"),
        t("diversity", k=k),
        t("novelty", k=k),
        t("head", n=beyond["popular_head"]),
    ]
    rows = [
        [
            method,
            number(values["coverage"]),
            f"{values['distinct']:,}",
            f"{values['never_played']:,}",
            number(values["diversity"]),
            number(values["novelty"]),
            f"{values['popular_head_share']:.1%}",
        ]
        for method, values in beyond["rows"].items()
    ]
    users = next(iter(beyond["rows"].values()))["users"]
    report.section(t("beyond", users=f"{users:,}", k=k), table(header, rows))
