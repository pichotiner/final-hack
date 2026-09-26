import type { Basis, Item } from "./api";

export type Pinned = { item: Item; basis: Basis; targetId: string | null };
export type SavedCommand = { path: string; body: string; actorId?: string };
export type ReviewState = {
  pinned: Pinned | null;
  latestRevision: number | null;
  pending: SavedCommand | null;
  phase: "idle" | "sending" | "unknown" | "rejected" | "succeeded";
  uncertain: boolean;
};
export const initialState: ReviewState = { pinned: null, latestRevision: null, pending: null, phase: "idle", uncertain: false };
export type Action =
  | { type: "pin"; value: Pinned }
  | { type: "poll"; revision: number }
  | { type: "prepare"; command: SavedCommand }
  | { type: "retry" }
  | { type: "unknown" }
  | { type: "rejected" }
  | { type: "success" }
  | { type: "clear" };

export function reviewReducer(state: ReviewState, action: Action): ReviewState {
  const locked = state.phase === "sending" || state.phase === "unknown";
  switch (action.type) {
    case "poll": return { ...state, latestRevision: Math.max(state.latestRevision ?? 0, action.revision) };
    case "pin":
      if (locked) return state;
      if (action.value.item.item_id !== action.value.basis.item_id ||
          action.value.item.context_revision !== action.value.basis.context_revision ||
          action.value.item.control_cycle_id !== action.value.basis.control_cycle_id ||
          action.value.basis.target_id !== action.value.targetId) {
        return state;
      }
      return { pinned: structuredClone(action.value), latestRevision: Math.max(state.latestRevision ?? 0, action.value.item.context_revision), pending: null, phase: "idle", uncertain: false };
    case "prepare":
      if (locked || !state.pinned || state.phase !== "idle") return state;
      if (state.latestRevision !== state.pinned.item.context_revision) return state;
      return { ...state, pending: { ...action.command }, phase: "sending" };
    case "retry":
      if (state.phase !== "unknown" || !state.pending) return state;
      return { ...state, phase: "sending" }; // Original bytes and key; never reconstruct from latest.
    case "unknown": return { ...state, phase: "unknown", uncertain: true };
    case "rejected": return { ...state, phase: state.uncertain ? "unknown" : "rejected" };
    case "success": return { ...state, phase: "succeeded", uncertain: false };
    case "clear":
      if (locked) return state;
      return { ...initialState };
  }
}

export function basisDiff(oldBasis: Basis, newBasis: Basis) {
  const oldIds = new Set(oldBasis.inputs.map(x => x.record_id));
  const newIds = new Set(newBasis.inputs.map(x => x.record_id));
  return {
    added: newBasis.inputs.filter(x => !oldIds.has(x.record_id)),
    removed: oldBasis.inputs.filter(x => !newIds.has(x.record_id)),
    changedRoles: newBasis.inputs.filter(x => oldBasis.inputs.some(y => y.record_id === x.record_id && (y.role !== x.role || y.reason !== x.reason))),
    changedVersions: Object.keys({ ...oldBasis.pinned_versions, ...newBasis.pinned_versions })
      .filter(key => oldBasis.pinned_versions[key] !== newBasis.pinned_versions[key]),
  };
}
