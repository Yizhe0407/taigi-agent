import type { ConversationState } from "../types"

/**
 * Named pose/expression vocabulary the Live2D avatar switches between. Coarser
 * than ConversationState — several conversation phases collapse onto the same
 * visual pose (see mapConversationStateToExpression below).
 */
export type ExpressionState = "idle" | "listening" | "thinking" | "talking" | "error"

export type ParamTransitionKind = "smooth" | "instant" | "delayed"

export type ParamTarget = {
  value: number
  transition: ParamTransitionKind
}

/**
 * Per-state Cubism parameter targets, ported from the AI站長 rig's standalone
 * control-panel test harness (webModel/src/components/ControlPanel.vue).
 * `ParamMouthOpenY` is intentionally absent from every state — that parameter
 * stays exclusively driven by live TTS/mic amplitude (officialCubismAvatar's
 * ambient loop) so lip-sync never fights with a static per-state mouth-open
 * value.
 *
 * Transition kinds, applied by OfficialCubismAvatar's transition engine:
 *   - "smooth":  ease in/out from the current value over EXPRESSION_TRANSITION_MS.
 *   - "instant": snap immediately — used for hand/gesture shape parameters,
 *                which have no meaningful "midway" pose.
 *   - "delayed": hold the current value until the transition would have
 *                finished, then snap — used for the error pose's crossed-arm
 *                GestureR/L so the fingers don't visibly clip through the
 *                body while the arm is still rotating into place.
 */
export const EXPRESSION_STATES: Record<ExpressionState, Record<string, ParamTarget>> = {
  idle: {
    EyeOpen: { value: 0.8, transition: "smooth" },
    ParamEyeLSmile: { value: 0, transition: "smooth" },
    ParamEyeRSmile: { value: 0, transition: "smooth" },
    ParamBrowRAngle: { value: 0, transition: "smooth" },
    ParamBrowRForm: { value: 0, transition: "smooth" },
    ParamBrowLAngle: { value: 0, transition: "smooth" },
    ParamBrowLForm: { value: 0, transition: "smooth" },
    ParamMouthForm: { value: 0, transition: "smooth" },
    MouthSmile: { value: 0.5, transition: "smooth" },
    Prayer: { value: 0, transition: "smooth" },
    JointShoulderR: { value: 0, transition: "smooth" },
    JointArmR: { value: 0, transition: "smooth" },
    JointHandR: { value: 0, transition: "smooth" },
    GestureR: { value: 0, transition: "instant" },
    JointShoulderL: { value: 0, transition: "smooth" },
    JointArmL: { value: 0, transition: "smooth" },
    JointHandL: { value: 0, transition: "smooth" },
    GestureL: { value: 0, transition: "instant" },
  },
  listening: {
    EyeOpen: { value: 1, transition: "smooth" },
    ParamEyeLSmile: { value: 0, transition: "smooth" },
    ParamEyeRSmile: { value: 0, transition: "smooth" },
    ParamBrowRAngle: { value: 0, transition: "smooth" },
    ParamBrowRForm: { value: 0.5, transition: "smooth" },
    ParamBrowLAngle: { value: 0, transition: "smooth" },
    ParamBrowLForm: { value: 0.5, transition: "smooth" },
    ParamMouthForm: { value: 0, transition: "smooth" },
    MouthSmile: { value: 0.5, transition: "smooth" },
    Prayer: { value: 0, transition: "smooth" },
    JointShoulderR: { value: -25, transition: "smooth" },
    JointArmR: { value: -90, transition: "smooth" },
    JointHandR: { value: -45, transition: "smooth" },
    GestureR: { value: 5, transition: "instant" },
    JointShoulderL: { value: -25, transition: "smooth" },
    JointArmL: { value: -30, transition: "smooth" },
    JointHandL: { value: 0, transition: "smooth" },
    GestureL: { value: 0, transition: "instant" },
  },
  thinking: {
    EyeOpen: { value: 0.8, transition: "smooth" },
    ParamEyeLSmile: { value: 0, transition: "smooth" },
    ParamEyeRSmile: { value: 0, transition: "smooth" },
    ParamBrowRAngle: { value: 0.45, transition: "smooth" },
    ParamBrowRForm: { value: 0.8, transition: "smooth" },
    ParamBrowLAngle: { value: 0.65, transition: "smooth" },
    ParamBrowLForm: { value: -0.8, transition: "smooth" },
    ParamMouthForm: { value: 0, transition: "smooth" },
    MouthSmile: { value: 0, transition: "smooth" },
    Prayer: { value: 0, transition: "smooth" },
    JointShoulderR: { value: 0, transition: "smooth" },
    JointArmR: { value: 35, transition: "smooth" },
    JointHandR: { value: 0, transition: "smooth" },
    GestureR: { value: 0, transition: "instant" },
    JointShoulderL: { value: 0, transition: "smooth" },
    JointArmL: { value: -90, transition: "smooth" },
    JointHandL: { value: 65, transition: "smooth" },
    GestureL: { value: 1, transition: "instant" },
  },
  talking: {
    EyeOpen: { value: 0.8, transition: "smooth" },
    ParamEyeLSmile: { value: 0.75, transition: "smooth" },
    ParamEyeRSmile: { value: 0.75, transition: "smooth" },
    ParamBrowLAngle: { value: 0, transition: "smooth" },
    ParamBrowLForm: { value: 0, transition: "smooth" },
    ParamBrowRAngle: { value: 0, transition: "smooth" },
    ParamBrowRForm: { value: 0, transition: "smooth" },
    ParamMouthForm: { value: 0, transition: "smooth" },
    MouthSmile: { value: 0.5, transition: "smooth" },
    Prayer: { value: 0, transition: "smooth" },
    JointShoulderR: { value: 0, transition: "smooth" },
    JointArmR: { value: -50, transition: "smooth" },
    JointHandR: { value: 50, transition: "smooth" },
    GestureR: { value: 5, transition: "instant" },
    JointShoulderL: { value: 5, transition: "smooth" },
    JointArmL: { value: -30, transition: "smooth" },
    JointHandL: { value: 0, transition: "smooth" },
    GestureL: { value: 0, transition: "instant" },
  },
  error: {
    EyeOpen: { value: 0.7, transition: "smooth" },
    ParamEyeLSmile: { value: 0, transition: "smooth" },
    ParamEyeRSmile: { value: 0, transition: "smooth" },
    ParamBrowRAngle: { value: 0, transition: "smooth" },
    ParamBrowRForm: { value: -0.2, transition: "smooth" },
    ParamBrowLAngle: { value: 0, transition: "smooth" },
    ParamBrowLForm: { value: -0.2, transition: "smooth" },
    ParamMouthForm: { value: -1, transition: "smooth" },
    MouthSmile: { value: -0.5, transition: "smooth" },
    Prayer: { value: 1, transition: "smooth" },
    JointShoulderR: { value: -80, transition: "smooth" },
    JointArmR: { value: 0, transition: "smooth" },
    JointHandR: { value: 0, transition: "smooth" },
    GestureR: { value: 6, transition: "delayed" },
    JointShoulderL: { value: 80, transition: "smooth" },
    JointArmL: { value: 0, transition: "smooth" },
    JointHandL: { value: 0, transition: "smooth" },
    GestureL: { value: 6, transition: "delayed" },
  },
}

/**
 * Collapses the 7-phase voice conversation state machine (useConversationState)
 * onto the 5-pose expression vocabulary above:
 *
 *   connecting   -> idle       (nothing to react to yet)
 *   listening    -> listening  (waiting for the user to start talking)
 *   userSpeaking -> listening  (VAD ground truth: user is talking — the avatar
 *                               keeps the attentive listening pose through it)
 *   processing   -> thinking   (short ASR/transcript gap; same pose as
 *                               thinking rather than flashing back to idle)
 *   thinking     -> thinking
 *   speaking     -> talking
 *   error        -> error
 */
export function mapConversationStateToExpression(state: ConversationState): ExpressionState {
  switch (state) {
    case "connecting":
      return "idle"
    case "listening":
    case "userSpeaking":
      return "listening"
    case "processing":
    case "thinking":
      return "thinking"
    case "speaking":
      return "talking"
    case "error":
      return "error"
  }
}
