/**
 * 面部特征提取（MediaPipe FaceLandmarker + blendshapes）。
 *
 * 对应 PRD §6.3.2：基础表情 + 微表情/持续状态特征 + 质量前置检测。
 *
 * 关键设计：
 * - 只在浏览器本地跑，原始视频帧**不上传**，只把 52 个 blendshape 数值发给后端。
 *   （PRD §8 数据最小化：面部图像仅做实时特征提取）
 * - WASM 模型加载失败时优雅降级为纯语音模式，不阻断会话。
 */

let faceLandmarker = null
let loading = null

const WASM_BASE =
  'https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/wasm'
const MODEL_URL =
  'https://storage.googleapis.com/mediapipe-models/face_landmarker/' +
  'face_landmarker/float16/1/face_landmarker.task'

export async function loadFaceModel() {
  if (faceLandmarker) return faceLandmarker
  if (loading) return loading

  loading = (async () => {
    const vision = await import('@mediapipe/tasks-vision')
    const { FaceLandmarker, FilesetResolver } = vision
    const fileset = await FilesetResolver.forVisionTasks(WASM_BASE)
    faceLandmarker = await FaceLandmarker.createFromOptions(fileset, {
      baseOptions: { modelAssetPath: MODEL_URL, delegate: 'GPU' },
      runningMode: 'VIDEO',
      numFaces: 2, // 检出 2 张脸 => 判定"多人入镜"
      outputFaceBlendshapes: true,
      outputFacialTransformationMatrixes: false,
    })
    return faceLandmarker
  })()
  return loading
}

/**
 * 分析一帧，返回 { faceDetected, blendshapes, faceCount, brightness }
 * 采样期间会累积多帧取平均，降低单帧抖动。
 */
export function createFaceSampler() {
  let acc = {}
  let frames = 0
  let faceFrames = 0
  let maxFaces = 0
  let brightnessSum = 0
  let lastTs = -1

  return {
    reset() {
      acc = {}
      frames = 0
      faceFrames = 0
      maxFaces = 0
      brightnessSum = 0
    },

    /** @param {HTMLVideoElement} video */
    sample(video) {
      if (!faceLandmarker || !video || video.readyState < 2) return
      const ts = Math.max(video.currentTime * 1000, lastTs + 1)
      lastTs = ts

      let res
      try {
        res = faceLandmarker.detectForVideo(video, ts)
      } catch {
        return
      }
      frames++
      maxFaces = Math.max(maxFaces, res.faceLandmarks?.length || 0)

      const shapes = res.faceBlendshapes?.[0]?.categories
      if (shapes && shapes.length) {
        faceFrames++
        for (const c of shapes) {
          acc[c.categoryName] = (acc[c.categoryName] || 0) + c.score
        }
      }

      // 亮度：从视频中心小区域粗估（避免整帧缩放大开销）
      try {
        const cv = document.createElement('canvas')
        cv.width = 32
        cv.height = 32
        const cx = cv.getContext('2d', { willReadFrequently: true })
        cx.drawImage(video, 0, 0, 32, 32)
        const d = cx.getImageData(0, 0, 32, 32).data
        let s = 0
        for (let i = 0; i < d.length; i += 4) {
          s += 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2]
        }
        brightnessSum += s / (d.length / 4) / 255
      } catch {
        /* 取不到就跳过 */
      }
    },

    /** 汇总本轮（一段发言）内的面部特征 */
    finish() {
      const blendshapes = {}
      if (faceFrames > 0) {
        for (const [k, v] of Object.entries(acc)) {
          blendshapes[k] = +(v / faceFrames).toFixed(3)
        }
      }
      const brightness = frames ? brightnessSum / frames : 0
      return {
        face_detected: faceFrames > 0,
        blendshapes,
        quality: {
          frames,
          face_frames: faceFrames,
          face_ratio: frames ? +(faceFrames / frames).toFixed(2) : 0,
          max_faces: maxFaces,
          brightness: +brightness.toFixed(3),
          low_light: brightness < 0.28,
          multi_person: maxFaces > 1,
        },
      }
    },
  }
}
