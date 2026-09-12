/**
 * 录音封装。
 *
 * 与 Web 端 MediaRecorder 的关键差异：
 * - 小程序只有 `wx.getRecorderManager()`，输出 mp3 / aac / pcm，**没有 webm/opus**；
 * - pcm 体积过大（16k/16bit/单声道 ≈ 32KB/s），不走公网上传，统一用 mp3；
 * - 必须先在 app.json 的 permission 里声明 scope.record，否则安卓直接失败。
 *
 * 参数与后端 `/api/mini/audio/formats` 的 recommended 保持一致。
 */
const OPTIONS = {
  duration: 120000, // 最长 2 分钟
  sampleRate: 16000,
  numberOfChannels: 1,
  encodeBitRate: 48000,
  format: 'mp3',
  frameSize: 1024, // 开启 onFrameRecorded 分片回调（做音量条用）
}

let manager = null
let onVolume = null

function ensureManager() {
  if (manager) return manager
  manager = wx.getRecorderManager()

  manager.onStart(() => {})
  manager.onError((err) => {
    console.error('[recorder] error', err)
    wx.hideLoading()
    wx.showToast({ title: '录音失败：' + (err.errMsg || '未知错误'), icon: 'none' })
  })
  // 分片回调：frameSize 有值时才有，用于画音量条
  manager.onFrameRecorded((res) => {
    if (!onVolume || !res.frameBuffer) return
    const buf = new Uint8Array(res.frameBuffer)
    let sum = 0
    for (let i = 0; i < buf.length; i += 2) {
      const v = (buf[i + 1] << 8) | buf[i]
      sum += Math.abs(v > 32767 ? v - 65536 : v)
    }
    const avg = buf.length ? sum / (buf.length / 2) : 0
    onVolume(Math.min(1, avg / 6000))
  })
  return manager
}

function authorize() {
  return new Promise((resolve, reject) => {
    wx.getSetting({
      success(res) {
        if (res.authSetting['scope.record'] === false) {
          wx.showModal({
            title: '需要麦克风权限',
            content: '语音能帮我更准确地理解你的状态。是否去设置页开启？',
            confirmText: '去设置',
            success(m) {
              if (m.confirm) {
                wx.openSetting({
                  success(s) {
                    s.authSetting['scope.record'] ? resolve() : reject(new Error('未授权麦克风'))
                  },
                  fail: () => reject(new Error('未授权麦克风')),
                })
              } else {
                reject(new Error('未授权麦克风'))
              }
            },
          })
          return
        }
        if (res.authSetting['scope.record']) return resolve()
        wx.authorize({
          scope: 'scope.record',
          success: resolve,
          fail: () => reject(new Error('未授权麦克风')),
        })
      },
      fail: () => reject(new Error('获取授权状态失败')),
    })
  })
}

/**
 * 开始录音。
 * @param {(v:number)=>void} volumeCb 音量回调，0~1
 */
function start(volumeCb) {
  return authorize().then(() => {
    onVolume = volumeCb || null
    ensureManager().start(OPTIONS)
  })
}

/** 停止录音，resolve 临时文件路径。 */
function stop() {
  return new Promise((resolve, reject) => {
    const m = ensureManager()
    m.onStop((res) => {
      onVolume = null
      // 太短的录音直接判无效，避免把空音频送去 ASR 产生幻觉文本
      if (!res.duration || res.duration < 800) {
        reject(new Error('说话时间太短了，请再说一次'))
        return
      }
      resolve({ filePath: res.tempFilePath, duration: res.duration })
    })
    m.stop()
  })
}

function cancel() {
  try {
    onVolume = null
    ensureManager().stop()
  } catch (e) {
    /* 未开始录音时 stop 会抛错，忽略 */
  }
}

module.exports = { start, stop, cancel, OPTIONS }
