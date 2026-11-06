/**
 * 浏览器语音合成（TTS）——零后端依赖，用 Web Speech API 让 AI 回复"开口说话"。
 *
 * 设计要点：
 * 1. speechSynthesis 在部分浏览器需要用户手势后才能触发（自动播放策略），
 *    因此 speak() 应由点击/说话这类用户动作链路调用。
 * 2. 打断 = cancel()；挂断通话前必须 cancel()，否则后台继续读。
 * 3. 中文音色：优先挑 lang 以 zh 开头的 voice，找不到就用默认。
 * 4. 兜底：浏览器不支持时 supported=false，界面提示但对话不中断。
 */

let _voices = []

export function ttsSupported() {
  return typeof window !== 'undefined' && 'speechSynthesis' in window
}

function pickChineseVoice() {
  if (!ttsSupported()) return null
  const voices = window.speechSynthesis.getVoices()
  _voices = voices
  return (
    voices.find((v) => /^zh/i.test(v.lang)) ||
    voices.find((v) => /cmn|Chinese|Mandarin/i.test(v.name)) ||
    voices[0] ||
    null
  )
}

// 预热：voices 在部分浏览器异步加载
export function warmupTTS() {
  if (!ttsSupported()) return
  window.speechSynthesis.getVoices()
  window.speechSynthesis.onvoiceschanged = () => {
    _voices = window.speechSynthesis.getVoices()
  }
}

/**
 * 朗读文本。
 * @param {string} text
 * @param {{rate?:number, pitch?:number, onStart?:Function, onEnd?:Function, onError?:Function}} opts
 * @returns {boolean} 是否真正开始朗读
 */
export function speak(text, opts = {}) {
  if (!ttsSupported() || !text) return false
  const synth = window.speechSynthesis
  synth.cancel() // 打断上一段
  const utter = new SpeechSynthesisUtterance(text)
  const voice = pickChineseVoice()
  if (voice) utter.voice = voice
  utter.lang = voice?.lang || 'zh-CN'
  utter.rate = opts.rate ?? 1.0
  utter.pitch = opts.pitch ?? 1.05
  utter.volume = 1.0
  if (opts.onStart) utter.onstart = opts.onStart
  if (opts.onEnd) utter.onend = opts.onEnd
  if (opts.onError) utter.onerror = opts.onError
  synth.speak(utter)
  return true
}

export function stopSpeak() {
  if (ttsSupported()) window.speechSynthesis.cancel()
}

export function isSpeaking() {
  return ttsSupported() && window.speechSynthesis.speaking
}
