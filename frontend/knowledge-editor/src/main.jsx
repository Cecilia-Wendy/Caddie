import React, { useEffect, useRef } from "react";
import { createRoot } from "react-dom/client";
import { MantineProvider } from "@mantine/core";
import { BlockNoteSchema, defaultBlockSpecs } from "@blocknote/core";
import { useCreateBlockNote } from "@blocknote/react";
import { BlockNoteView } from "@blocknote/mantine";
import "@blocknote/core/fonts/inter.css";
import "@blocknote/mantine/style.css";
import "@mantine/core/styles.css";
import "./style.css";

const schema = BlockNoteSchema.create({ blockSpecs: defaultBlockSpecs });

function KnowledgeEditor({ initialMarkdown, onChange, onReady }) {
  const changeRef = useRef(onChange);
  const userActiveRef = useRef(false);
  changeRef.current = onChange;
  const editor = useCreateBlockNote({ schema });

  useEffect(() => {
    let active = true;
    (async () => {
      const blocks = await editor.tryParseMarkdownToBlocks(initialMarkdown || "");
      if (!active) return;
      editor.replaceBlocks(editor.document, blocks.length ? blocks : [{ type: "paragraph" }]);
      onReady?.(editor);
    })();
    return () => { active = false; };
  }, [editor, initialMarkdown, onReady]);

  const activate = () => { userActiveRef.current = true; };
  return <div onBeforeInput={activate} onPaste={activate} onDrop={activate} onKeyDown={activate} onPointerDown={activate}>
    <BlockNoteView
      editor={editor}
      theme="light"
      slashMenu
      formattingToolbar
      onChange={async () => {
        if (!userActiveRef.current) return;
        changeRef.current?.(await editor.blocksToMarkdownLossy(editor.document));
      }}
    />
  </div>;
}

function mount(element, options = {}) {
  const root = createRoot(element);
  root.render(
    <MantineProvider defaultColorScheme="light">
      <KnowledgeEditor {...options} />
    </MantineProvider>
  );
  return { destroy: () => root.unmount() };
}

window.CaddieKnowledgeEditor = { mount };
