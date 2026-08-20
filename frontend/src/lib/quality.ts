import type { QualityIssue } from "@/lib/api";

export const QUALITY_SEVERITY_LABELS: Record<string, string> = {
  info: "提示",
  warning: "警告",
  high: "高风险",
  critical: "严重",
};

export const QUALITY_DIMENSION_LABELS: Record<string, string> = {
  parse_completeness: "解析完整性",
  text_integrity: "文本完整性",
  structure_integrity: "结构完整性",
  duplication: "内容重复度",
  information_density: "信息密度",
  context_completeness: "上下文完整性",
  source_traceability: "来源可追溯性",
  retrieval_readiness: "检索可用性",
};

export const QUALITY_ISSUE_LABELS: Record<string, string> = {
  NO_VALID_TEXT: "未解析出有效文本",
  SUSPICIOUSLY_SPARSE_EXTRACTION: "解析出的文本明显偏少",
  UNRECOVERABLE_TEXT_CORRUPTION: "文本损坏严重，无法可靠使用",
  HIGH_TEXT_CORRUPTION: "文本乱码比例过高",
  TEXT_CORRUPTION_DETECTED: "检测到少量乱码字符",
  HIGH_DUPLICATION: "重复内容比例过高",
  DUPLICATION_DETECTED: "检测到重复或近似重复内容",
  SOURCE_TRACEABILITY_LOW: "大量内容缺少来源位置",
  SOURCE_LOCATOR_GAPS: "部分内容缺少来源位置",
  HIGH_EMPTY_PAGE_RATIO: "空白解析页面比例过高",
  OCR_CONFIDENCE_LOW: "低置信度识别内容过多",
  OCR_CONFIDENCE_WARNING: "存在低置信度识别内容",
  INFORMATION_DENSITY_LOW: "文档有效信息密度过低",
  INFORMATION_DENSITY_WARNING: "文档有效信息密度偏低",
  HEADING_LEVEL_JUMP: "标题层级存在跳级",
};

export const QUALITY_ISSUE_DESCRIPTIONS: Record<string, string> = {
  HIGH_DUPLICATION: "重复或近似重复的内容块比例超过隔离阈值，可能造成检索结果重复并放大同一信息。",
  DUPLICATION_DETECTED: "文档中存在重复或近似重复的内容块，建议检查是否为正常页眉页脚、模板内容或重复正文。",
  SOURCE_TRACEABILITY_LOW: "过多内容块缺少页码、行号或工作表等可用来源位置，回答引用可能无法准确回到原文。",
  SOURCE_LOCATOR_GAPS: "部分内容块缺少页码、行号或工作表等来源位置，请确认这是否影响引用核验。",
  SUSPICIOUSLY_SPARSE_EXTRACTION: "相对源文件大小，解析出的文字明显偏少，可能存在扫描页、图片文字或解析遗漏。",
  HIGH_TEXT_CORRUPTION: "解析文本包含较多替换符或方框字符，可能影响检索与回答准确性。",
  UNRECOVERABLE_TEXT_CORRUPTION: "文本损坏比例已超过拒绝阈值，当前内容不适合进入知识库。",
  TEXT_CORRUPTION_DETECTED: "解析文本中仍有少量替换符或方框字符，请抽查其是否影响关键内容。",
  HIGH_EMPTY_PAGE_RATIO: "较多源文件页面没有提取到有效文字，可能存在解析遗漏。",
  OCR_CONFIDENCE_LOW: "较多内容的文字识别置信度偏低，建议对照源文件核查关键段落。",
  OCR_CONFIDENCE_WARNING: "部分内容的文字识别置信度偏低，建议抽查受影响内容。",
  INFORMATION_DENSITY_LOW: "文本中有效字词占比过低，可能包含大量符号、噪声或无意义内容。",
  INFORMATION_DENSITY_WARNING: "文本有效信息占比偏低，请确认内容是否仍具有检索价值。",
  HEADING_LEVEL_JUMP: "标题层级出现跳级，可能导致章节关系或分块上下文不准确。",
};

export const QUALITY_REVIEW_GUIDANCE: Record<string, string> = {
  HIGH_DUPLICATION: "抽查重复片段。如果只是页眉、页脚或合法模板，可考虑放行；如果正文被整段重复，建议驳回并重新处理。",
  DUPLICATION_DETECTED: "确认重复内容属于版式噪声还是正文重复，重点检查检索时是否会反复返回同一信息。",
  SOURCE_TRACEABILITY_LOW: "抽查关键段落能否回到准确页码、行号或工作表。无法定位来源会直接影响答案引用的可核验性。",
  SOURCE_LOCATOR_GAPS: "重点检查业务关键内容是否具备来源位置，少量非关键装饰文本缺失通常不影响放行。",
  SUSPICIOUSLY_SPARSE_EXTRACTION: "对照源文件页数与解析文本，确认扫描页、图片或表格中的重要信息是否遗漏。",
  HIGH_TEXT_CORRUPTION: "抽查乱码附近是否包含名称、数字、日期等关键信息；关键内容受损时应驳回。",
  UNRECOVERABLE_TEXT_CORRUPTION: "当前文本不具备可靠使用条件，建议驳回并更换解析方式。",
  TEXT_CORRUPTION_DETECTED: "检查少量乱码是否落在关键条款中；仅影响无关符号时可考虑放行。",
  HIGH_EMPTY_PAGE_RATIO: "对照空白页是否确实无正文；若源页有内容但未解析，应驳回并重新处理。",
  OCR_CONFIDENCE_LOW: "对照源文件核查低置信度段落，尤其关注数字、专有名词和表格数据。",
  OCR_CONFIDENCE_WARNING: "抽查受影响段落，确认文字识别错误不会改变原意。",
  INFORMATION_DENSITY_LOW: "确认文档是否本身以图片、表格或代码为主，或解析结果混入了大量无意义符号。",
  INFORMATION_DENSITY_WARNING: "抽查低信息密度区域，判断是否仍能形成有价值的检索内容。",
  HEADING_LEVEL_JUMP: "检查章节归属是否正确，确认标题跳级没有让正文挂到错误的上级章节。",
};

export const QUALITY_METRIC_LABELS: Record<string, string> = {
  valid_text_char_count: "有效文本字符数",
  extracted_chars_per_mb: "每 MB 提取字符数",
  corruption_ratio: "乱码字符占比",
  duplicate_block_ratio: "重复内容块占比",
  missing_locator_ratio: "缺少来源位置的内容块占比",
  empty_page_ratio: "无有效文本页面占比",
  low_confidence_block_ratio: "低置信度内容块占比",
  information_density: "有效信息占比",
  heading_level_jump_count: "标题跳级数量",
};

const QUALITY_RATIO_METRICS = new Set([
  "corruption_ratio",
  "duplicate_block_ratio",
  "missing_locator_ratio",
  "empty_page_ratio",
  "low_confidence_block_ratio",
  "information_density",
]);

export function qualityIssueTitle(issue: QualityIssue) {
  return QUALITY_ISSUE_LABELS[issue.code] ?? issue.message;
}

export function qualityIssueDescription(issue: QualityIssue) {
  return QUALITY_ISSUE_DESCRIPTIONS[issue.code] ?? issue.message;
}

export function affectedBlockIds(issue: QualityIssue) {
  return [...new Set(issue.evidence.flatMap((evidence) => evidence.block_ids ?? []))];
}

export function formatQualityValue(metric: string, value: unknown) {
  if (value == null) return "—";
  if (typeof value === "string" || typeof value === "boolean") return String(value);
  if (typeof value !== "number") return JSON.stringify(value);
  if (QUALITY_RATIO_METRICS.has(metric)) return `${(value * 100).toFixed(value * 100 < 1 ? 2 : 1)}%`;
  return Number.isInteger(value) ? String(value) : value.toFixed(3);
}
