(function () {
  if (typeof window.markdownit !== "function") return;

  const parser = window.markdownit({ html: false, breaks: true, linkify: true });
  const localOrigin = "https://markdown.invalid";

  function linkKind(href) {
    if (!href || /[\\\x00-\x20\x7f]/.test(href)) return null;

    if (/^https?:\/\//i.test(href)) {
      try {
        return new URL(href).hostname ? "external" : null;
      } catch {
        return null;
      }
    }

    // Treat encoded scheme separators and network paths the same as literal ones.
    const path = href.split(/[?#]/, 1)[0];
    const decodedPath = path.replace(/%([\da-f]{2})/gi, (_, hex) => (
      String.fromCharCode(Number.parseInt(hex, 16))
    ));
    if (/^[a-z][\da-z+.-]*:|^\/\/|[\\\x00-\x1f\x7f]/i.test(decodedPath.trimStart())) {
      return null;
    }

    try {
      return new URL(href, localOrigin).origin === localOrigin ? "local" : null;
    } catch {
      return null;
    }
  }

  parser.validateLink = (href) => linkKind(href) !== null;

  parser.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
    const token = tokens[index];
    if (linkKind(token.attrGet("href")) === "external") {
      token.attrSet("target", "_blank");
      token.attrSet("rel", "noopener noreferrer");
    }
    return renderer.renderToken(tokens, index, options);
  };

  parser.renderer.rules.image = (tokens, index, options, env, renderer) => {
    const alt = renderer.renderInlineAsText(tokens[index].children, options, env);
    return parser.utils.escapeHtml(alt);
  };

  parser.renderer.rules.table_open = (tokens, index, options, env, renderer) => (
    '<div class="markdown-table">' + renderer.renderToken(tokens, index, options)
  );
  parser.renderer.rules.table_close = (tokens, index, options, env, renderer) => (
    renderer.renderToken(tokens, index, options) + "</div>\n"
  );

  window.thoughtMarkdown = {
    render(text) {
      return parser.render(String(text ?? ""));
    },
    plainText(text) {
      const env = {};
      const parts = [];
      for (const token of parser.parse(String(text ?? ""), env)) {
        if (token.type === "inline") {
          parts.push(parser.renderer.renderInlineAsText(token.children, parser.options, env));
        } else if (token.type === "fence" || token.type === "code_block") {
          parts.push(token.content);
        }
      }
      return parts.join(" ");
    },
  };
})();
