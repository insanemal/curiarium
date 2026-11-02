import React, { useRef, useState } from "react";

import { entriesFromDataTransfer } from "../../lib/uploadTree";
import type { Uploader } from "../useUpload";

/**
 * What the current Files scope does with a drag: accept the upload and name the
 * destination, refuse it with a reason, or stay inert (project and missing
 * mode, where a drop means nothing at all).
 */
export type DropPolicy =
  | { kind: "upload"; name: string }
  | { kind: "refuse"; message: string }
  | { kind: "inert" };

type AssetDropOptions = {
  policy: DropPolicy;
  /** Roles below contributor get no drop handling: a drop here is an upload. */
  canContribute: boolean;
  uploader: Uploader;
};

export type AssetDropApi = {
  dragActive: boolean;
  dropUploading: boolean;
  showDropOverlay: boolean;
  dropMessage: string;
  handleDragEnter: (e: React.DragEvent<HTMLDivElement>) => void;
  handleDragOver: (e: React.DragEvent<HTMLDivElement>) => void;
  handleDragLeave: (e: React.DragEvent<HTMLDivElement>) => void;
  handleDrop: (e: React.DragEvent<HTMLDivElement>) => Promise<void>;
};

/**
 * Drag-and-drop upload for the grid's root element. The handlers stay bound to
 * that element: `dragDepth` counts enter/leave pairs, so the same node hierarchy
 * must see all four events.
 */
export function useAssetDrop({ policy, canContribute, uploader }: AssetDropOptions): AssetDropApi {
  const dragDepth = useRef(0);
  const [dragActive, setDragActive] = useState(false);
  const [dropUploading, setDropUploading] = useState(false);

  const isFileDrag = (e: React.DragEvent<HTMLDivElement>) => {
    const types = Array.from(e.dataTransfer?.types || []);
    return types.includes("Files");
  };

  // "inert" is the project/missing case: no overlay, no message, no state.
  const engaged = (e: React.DragEvent<HTMLDivElement>) =>
    canContribute && policy.kind !== "inert" && isFileDrag(e);

  const handleDragEnter = (e: React.DragEvent<HTMLDivElement>) => {
    if (!engaged(e)) return;
    // A refusal shows the overlay without cancelling anything; `handleDragOver`
    // carries the spec reason, since its cancellation is what decides legality.
    if (policy.kind === "upload") {
      e.preventDefault();
      e.stopPropagation();
    }
    // `dragDepth` is shared by both branches so a child-to-child move does not
    // flicker the overlay off and on.
    dragDepth.current += 1;
    setDragActive(true);
  };

  const handleDragOver = (e: React.DragEvent<HTMLDivElement>) => {
    if (!engaged(e)) return;
    // `preventDefault` on dragover is what makes a drop legal, so a refusal that
    // cancelled anything would be a lie: the browser shows the no-drop cursor
    // and, per the HTML drag-and-drop spec, never dispatches `drop` on an
    // uncancelled target. jsdom dispatches whatever we fire, so the tests can
    // only prove that these handlers cancel nothing and upload nothing.
    if (policy.kind !== "upload") return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  };

  const handleDragLeave = (e: React.DragEvent<HTMLDivElement>) => {
    if (!engaged(e)) return;
    if (policy.kind === "upload") {
      e.preventDefault();
      e.stopPropagation();
    }
    dragDepth.current -= 1;
    if (dragDepth.current <= 0) {
      dragDepth.current = 0;
      setDragActive(false);
    }
  };

  const handleDrop = async (e: React.DragEvent<HTMLDivElement>) => {
    if (!engaged(e)) return;
    if (policy.kind === "refuse") {
      // Nothing was accepted, so nothing is uploaded and the drag is not
      // cancelled; only the refusal state is reset.
      dragDepth.current = 0;
      setDragActive(false);
      return;
    }
    e.preventDefault();
    e.stopPropagation();
    dragDepth.current = 0;
    setDragActive(false);
    setDropUploading(true);
    try {
      const entries = await entriesFromDataTransfer(e.dataTransfer);
      if (!entries.length) return;
      await uploader.upload(entries);
    } finally {
      setDropUploading(false);
    }
  };

  const showDropOverlay =
    canContribute && policy.kind !== "inert" && (dragActive || dropUploading);
  let dropMessage = "";
  if (policy.kind === "refuse") {
    dropMessage = policy.message;
  } else if (policy.kind === "upload") {
    dropMessage = dropUploading
      ? `Uploading to ${policy.name}...`
      : `Drop files or folders to upload to ${policy.name}`;
  }

  return {
    dragActive,
    dropUploading,
    showDropOverlay,
    dropMessage,
    handleDragEnter,
    handleDragOver,
    handleDragLeave,
    handleDrop,
  };
}
