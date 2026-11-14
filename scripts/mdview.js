// Rendered Markdown view of diffview pages: Markdown to HTML with the vendored marked, limited to safe HTML.
// Raw HTML passes only for a short list of tags and keeps no attribute except class; links open only for
// http(s) and mailto; images become placeholders (the page cannot load files next to the document).
// Works in the browser (window.DVMarkdown) and in node for the tests (globalThis.DVMarkdown).
(function (root) {
  'use strict';
  var ALLOWED = {ins: 1, del: 1, br: 1, div: 1, p: 1, span: 1, sub: 1, sup: 1, kbd: 1, details: 1, summary: 1,
    mark: 1, small: 1, b: 1, i: 1, em: 1, strong: 1, code: 1, pre: 1, u: 1, s: 1, hr: 1};

  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function sanitize(raw) {
    return String(raw).replace(/<!--[\s\S]*?-->/g, '').replace(/<(\/?)([A-Za-z][\w-]*)([^<>]*)>|[<>]/g,
      function (m, slash, name, attrs) {
        if (!name || !ALLOWED[name.toLowerCase()]) {
          return esc(m);
        }
        name = name.toLowerCase();
        if (slash) {
          return '</' + name + '>';
        }
        var cls = /(?:^|\s)class\s*=\s*"([\w\- ]*)"/.exec(attrs || '');
        return '<' + name + (cls ? ' class="' + cls[1] + '"' : '') + (/\/\s*$/.test(attrs || '') ? ' /' : '') + '>';
      });
  }

  var md = new root.marked.Marked({gfm: true, breaks: false});
  md.use({
    renderer: {
      html: function (token) {
        return sanitize(token.text);
      },
      link: function (token) {
        var text = this.parser.parseInline(token.tokens);
        var href = token.href || '';
        if (/^(https?:|mailto:)/i.test(href)) {
          return '<a href="' + esc(href) + '" target="_blank" rel="noopener noreferrer">' + text + '</a>';
        }
        return '<span class="md-link" title="' + esc(href) + '">' + text + '</span>';
      },
      image: function (token) {
        var label = (root.DVT && root.DVT.image) || 'Image';
        return '<span class="md-img" title="' + esc(token.href || '') + '">' + esc(label) + ': ' + esc(token.text || '') + '</span>';
      }
    }
  });

  root.DVMarkdown = {
    render: function (src) {
      return md.parse(String(src));
    },
    sanitize: sanitize
  };
}(typeof window !== 'undefined' ? window : globalThis));
