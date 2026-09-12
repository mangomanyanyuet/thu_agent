/** 展示层的枚举 -> 中文映射。与后端 config.RISK_LEVELS / models 保持一致。 */

const RISK_META = {
  none: { label: '平稳', cls: 'tag-none' },
  low: { label: '轻度', cls: 'tag-low' },
  moderate: { label: '中度', cls: 'tag-moderate' },
  high: { label: '重度', cls: 'tag-high' },
  imminent: { label: '危急', cls: 'tag-imminent' },
}

const EMOTION_LABEL = {
  joy: '愉悦',
  calm: '平静',
  neutral: '中性',
  sadness: '低落',
  anxiety: '焦虑',
  anger: '愤怒',
  fear: '恐惧',
  shame: '羞耻/自责',
  numbness: '麻木',
}

const CBT_STAGE_LABEL = {
  none: '自由陪伴',
  affect_labeling: '情绪命名',
  situation: '情境澄清',
  automatic_thought: '识别自动化思维',
  distortion: '认知歪曲分类',
  socratic: '苏格拉底提问',
  alternative: '替代性想法',
  behavior: '行为激活',
  wrapup: '小结反馈',
}

const CHANNEL_LABEL = {
  rule: '文本规则',
  model: '模型语义',
  multimodal: '语音/多模态',
  scale: '量表条目',
}

const SOURCE_LABEL = {
  rule: '文本规则',
  model: '模型语义',
  multimodal: '多模态',
  scale_item: '量表条目',
}

function riskMeta(level) {
  return RISK_META[level] || { label: level || '未知', cls: 'tag-none' }
}

function emotionLabel(key) {
  if (!key) return ''
  return EMOTION_LABEL[key] || key
}

function cbtLabel(stage) {
  if (!stage || stage === 'none') return ''
  return CBT_STAGE_LABEL[stage] || stage
}

function channelLabel(k) {
  return CHANNEL_LABEL[k] || k
}

function sourceLabel(k) {
  return SOURCE_LABEL[k] || k
}

/** 毫秒 -> "3.2s" */
function ms(v) {
  if (v == null) return ''
  return v >= 1000 ? (v / 1000).toFixed(1) + 's' : v + 'ms'
}

/** ISO 时间 -> "09-04 15:20" */
function shortTime(iso) {
  if (!iso) return ''
  const s = String(iso).replace('T', ' ').slice(0, 16)
  return s.length > 10 ? s.slice(5) : s
}

module.exports = {
  RISK_META,
  riskMeta,
  emotionLabel,
  cbtLabel,
  channelLabel,
  sourceLabel,
  ms,
  shortTime,
}
