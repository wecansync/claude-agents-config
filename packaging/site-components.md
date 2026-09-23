# Docs component library

Read this before writing a section in `site/docs/index.html`. Every
component below is already styled by `site/assets/site.css`. Do not add new
CSS or JS; compose sections from these exact markup and class patterns. The
fully-worked example of all of them together is the `#install` section in
`site/docs/index.html` — copy patterns from there.

This file is repository-only (under `packaging/`) and is never deployed or
linked from the site. Keep the docs page itself free of authoring notes.

## 1. Section + heading with self-anchor link

```html
<section id="SECTION-ID">
  <h2 class="anchor-heading">Title<a class="anchor-link" href="#SECTION-ID" aria-label="Link to this section">#</a></h2>
  ...
</section>
```

For a sub-heading inside a section that needs its own deep link, give the
`h3` its own unique id and point the anchor at that id:

```html
<h3 id="SECTION-ID-sub" class="anchor-heading">Sub title<a class="anchor-link" href="#SECTION-ID-sub" aria-label="Link to this section">#</a></h3>
```

Sidebar entries in `.docs-nav` must reference top-level `SECTION-ID`s only.

## 2. Paragraph

```html
<p>Plain text, with <a href="/some/path">links</a> and <code class="il">inline code</code>.</p>
```

## 3. Inline code

```html
<code class="il">flag-or-path-or-value</code>
```

## 4. OS / choice tabs

The id prefix must be unique per tab group on the page.

```html
<div class="tabs" id="UNIQUE-tabs">
  <div role="tablist" aria-label="Operating system">
    <button role="tab" id="tab-UNIQUE-a" aria-controls="panel-UNIQUE-a">Label A</button>
    <button role="tab" id="tab-UNIQUE-b" aria-controls="panel-UNIQUE-b">Label B</button>
  </div>
  <div role="tabpanel" id="panel-UNIQUE-a" aria-labelledby="tab-UNIQUE-a">
    <h3 class="panel-label">Label A</h3>
    <div class="code-wrap">
      <pre><code>the command</code></pre>
      <button class="copy-btn" type="button" aria-label="Copy Label A command">Copy</button>
    </div>
  </div>
  <div role="tabpanel" id="panel-UNIQUE-b" aria-labelledby="tab-UNIQUE-b">
    <h3 class="panel-label">Label B</h3>
    <div class="code-wrap"><!-- same pattern --></div>
  </div>
</div>
```

The `.panel-label` heading in each panel is hidden by CSS once JS is active
(`site.js` adds a `js` class to `<html>`); keep it in every panel since it is
the only panel label without JS. Do not add a `hidden` attribute in static
markup — `site.js` adds and removes it itself. Without JS every panel must
stay visible (constraint: the site works without JS).

## 5. Code block with copy button (no tabs)

```html
<div class="code-wrap">
  <pre><code>command --flag value</code></pre>
  <button class="copy-btn" type="button" aria-label="Copy THING command">Copy</button>
</div>
```

Multi-line: put literal newlines inside `<code>...</code>`; comments start
with `#`.

## 6. Table

```html
<div class="table-wrap">
  <table>
    <thead><tr><th>Column</th><th>Column</th></tr></thead>
    <tbody>
      <tr><td><code class="il">value</code></td><td>Description.</td></tr>
    </tbody>
  </table>
</div>
```

## 7. Callouts — three variants: note, tip, warn

```html
<aside class="callout note"><span class="callout-label">Note</span><p>Text.</p></aside>
<aside class="callout tip"><span class="callout-label">Tip</span><p>Text.</p></aside>
<aside class="callout warn"><span class="callout-label">Warning</span><p>Text.</p></aside>
```

## 8. Numbered step list

```html
<ol class="step-list">
  <li><h4>Step title</h4><p>Step body text.</p></li>
  <li><h4>Step title</h4><p>Step body text.</p></li>
</ol>
```

## 9. Command-reference list (command + one-line description, not a table)

```html
<div class="cmd-ref">
  <div class="cmd-ref-item"><code class="il">the command</code><p>What it does.</p></div>
  <div class="cmd-ref-item"><code class="il">another command</code><p>What it does.</p></div>
</div>
```

## 10. Requirements / checkmark list

```html
<ul class="req-list">
  <li><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg> Item text</li>
</ul>
```

## 11. Lane grid (for the "lanes" section)

```html
<div class="lane-grid">
  <div class="lane-card">
    <div class="lane-name-row"><span class="lane-name">fleet-x</span><span class="tag tag-ro">Read-only</span></div>
    <p class="lane-desc">Role text.</p>
  </div>
</div>
```

Use `class="tag tag-rw"` + text "Writable" for writable lanes.

## 12. FAQ item (details/summary, matches landing page pattern)

```html
<details class="faq-item"><summary>Question?</summary><p>Answer.</p></details>
```

Wrap a group of these in `<div class="faq-list">...</div>`.

## Rules

Do not introduce new class names. If a section needs something not listed
here, ask before inventing new CSS. Never write the literal characters
`<!--` or `-->` inside an HTML comment block in the actual site files
(including inside example markup shown inside a comment) — the first `-->`
anywhere closes the whole comment early and everything after it renders as
live page content. This is exactly what happened during the first draft of
`site/docs/index.html`: describe comments in prose instead of showing them
literally if you ever need to reference one from inside a real HTML comment.
