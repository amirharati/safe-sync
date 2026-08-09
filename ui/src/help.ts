import userGuideMarkdown from "../../docs/user-guide.md?raw";

function appendInlineText(parent: HTMLElement, source: string): void {
  const pattern = /(`[^`]+`|\*\*[^*]+\*\*)/g;
  let offset = 0;
  for (const match of source.matchAll(pattern)) {
    const index = match.index ?? 0;
    if (index > offset) parent.append(document.createTextNode(source.slice(offset, index)));
    const token = match[0];
    const element = document.createElement(token.startsWith("`") ? "code" : "strong");
    element.textContent = token.startsWith("`") ? token.slice(1, -1) : token.slice(2, -2);
    parent.append(element);
    offset = index + token.length;
  }
  if (offset < source.length) parent.append(document.createTextNode(source.slice(offset)));
}

export function renderUserGuide(root: HTMLElement): void {
  root.replaceChildren();
  let paragraph: string[] = [];
  let list: HTMLOListElement | HTMLUListElement | null = null;
  let lastListItem: HTMLLIElement | null = null;
  let listOrdered = false;
  let code: string[] | null = null;

  const flushParagraph = (): void => {
    if (paragraph.length === 0) return;
    const element = document.createElement("p");
    appendInlineText(element, paragraph.join(" "));
    root.append(element);
    paragraph = [];
  };

  const closeList = (): void => {
    list = null;
    lastListItem = null;
  };

  for (const line of userGuideMarkdown.split("\n")) {
    if (line.startsWith("```")) {
      flushParagraph();
      closeList();
      if (code === null) {
        code = [];
      } else {
        const pre = document.createElement("pre");
        const element = document.createElement("code");
        element.textContent = code.join("\n");
        pre.append(element);
        root.append(pre);
        code = null;
      }
      continue;
    }
    if (code !== null) {
      code.push(line);
      continue;
    }

    if (/^\s{2,}\S/.test(line) && list !== null && lastListItem !== null) {
      lastListItem.append(document.createTextNode(" "));
      appendInlineText(lastListItem, line.trim());
      continue;
    }

    const heading = /^(#{1,3})\s+(.+)$/.exec(line);
    if (heading) {
      flushParagraph();
      closeList();
      const level = Math.min(4, heading[1].length + 1);
      const element = document.createElement(`h${level}`);
      element.textContent = heading[2];
      root.append(element);
      continue;
    }

    const unorderedItem = /^-\s+(.+)$/.exec(line);
    const orderedItem = /^\d+\.\s+(.+)$/.exec(line);
    if (unorderedItem || orderedItem) {
      flushParagraph();
      const ordered = Boolean(orderedItem);
      if (list === null || listOrdered !== ordered) {
        list = document.createElement(ordered ? "ol" : "ul");
        listOrdered = ordered;
        root.append(list);
      }
      const item = document.createElement("li");
      appendInlineText(item, (orderedItem ?? unorderedItem)?.[1] ?? "");
      list.append(item);
      lastListItem = item;
      continue;
    }

    if (line.trim() === "") {
      flushParagraph();
      closeList();
    } else {
      closeList();
      paragraph.push(line.trim());
    }
  }
  flushParagraph();
}
