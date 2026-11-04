// SPDX-License-Identifier: AGPL-3.0-or-later
// Runs in every page of the recorder's browser (an init script). In the mode record it turns
// what the person does into steps (click, fill, press Enter); in browse it lets them go
// anywhere unrecorded. In a picking mode the element clicked becomes a selector: for links to
// follow and file links one that matches the similar links too (with all of them, for the
// recorder to propose a pattern), for the next page's link one that matches only it (and, when
// it is a number among numbers, 1 2 3 …, the numbers' container: numbered pages). A pick may be
// an element without an address (a button, a row or card a script opens, an <a> without href or
// with javascript:): its similar ones are reported with no address, and the recorder makes the
// level one that is clicked (matched by the elements' texts).
// highlight(selector, pattern, hits) outlines the selector's links: solid where the pattern
// matches their URL (or Python found it to, hits), dashed where it does not. Everything goes to
// the recorder through window.__harvesterReport.
//
// A step's element and the next page's link are found by several selectors, each matching it
// alone (candidates), ranked as Playwright's codegen ranks them: a test id, a stable id, its role
// and accessible name, its aria-label, its name, its stable classes, its text, its place among
// its siblings last; the first is the step's selector, the others its alternatives for a replay
// to try when the site changed (harvester.selectors). Links to follow and file links get
// alternatives that match the same links. Role and text selectors are Playwright's
// (internal:role=link[name="…"s], a:text-is("…")): query() finds them here as Playwright does
// for the elements they are made for, and counts any element it is unsure of as a match, so that
// a selector is kept only when it is certain to find the element alone.
//
// In the mode value the person clicks a piece of text near one of a level's links (valueLinks:
// their selector): a date in the link's row, a title in its card. The pick says where it is from
// the link, as the job finds it beside every link: the closest ancestor of the link that is the
// row or card (one that holds that link alone), then the element in it, and its text.
//
// In the mode tags the person clicks one tag chip in a link's row or card: the chips alike in each
// card become a list value (many). On a page a level's links open, page-value picks one element
// (a value of that page) and page-tags a chip and those like it (as links are picked), outlined.
//
// In the mode until the person clicks the element an approval or a handover waits for (a list of
// accounts after the sign-in): the pick is a CSS selector that document.querySelector finds it
// by, as the job looks for it (harvester.sources.attend).
//
// It runs in every frame too (a consent banner's, a sign-in's), cross-origin ones included: the
// recorder learns from the report itself which frame it came from, and asks the frame's parent
// for its element's selector (frameSelector). A frame reports when the mouse or the focus comes
// into it ("where"), for the recorder to show where the person acts.
//
// Besides clicks and fills it records a list's choice (select), the keys that work a list of
// suggestions, text edited in place, and the menu a click's element showed under (hover). It
// leaves out what the browser does of itself (a label's click passed on to its checkbox, the
// submit button Enter clicks), never reports what a field that was ever a password's holds, and
// refuses files to upload ("refused"). Elements in open shadow roots are recorded with selectors
// that go into them.
(() => {
	// Once per document: a popup opened at an address of its opener's origin keeps the window of
	// its first, empty document (about:blank) for the page it loads, which needs its listeners.
	if (window.__harvester && window.__harvester.document === document) return
	const state = { mode: 'record', highlighted: [], document }
	window.__harvester = state

	const report = (event) => window.__harvesterReport && window.__harvesterReport(event)
	const css = (value) => CSS.escape(value)
	// Classes that change with the state or look generated say nothing stable about an element.
	const unstable = /(^|[-_])(active|current|hover|focus|selected|open|visible|show|hidden|disabled)([-_]|$)|\d{3,}|^css-|^sc-|^jsx-/i
	const TEST_IDS = ['data-testid', 'data-test', 'data-qa', 'data-cy', 'data-test-id']
	// Most candidates a step keeps: the best and its alternatives.
	const MOST = 5

	function classes(el) {
		return [...el.classList].filter((name) => !unstable.test(name)).slice(0, 3)
	}

	// An id or a value a framework or a build made (React's :r1:, ember123, a hash, a long number)
	// changes from visit to visit or release to release: it finds nothing the next time.
	function generated(value) {
		if (!value || value.length > 60) return true
		if (/\d{3,}|^:|:$|[0-9a-f]{8,}|^(ember|ext-gen|yui|gwt|ui-id|mui|radix|headlessui|react-select|__)/i.test(value)) return true
		// Letters and digits in turns (x7k2q9) read as random.
		return (value.match(/\d+/g) || []).length >= 3
	}

	const normal = (text) => (text || '').replace(/\s+/g, ' ').trim()

	// The roles a role selector is made for, and the elements that have them.
	const ROLES = {
		link: 'a[href], [role=link]',
		button: 'button, input[type=submit], input[type=button], input[type=reset], [role=button]',
		checkbox: 'input[type=checkbox], [role=checkbox]',
		radio: 'input[type=radio], [role=radio]',
		textbox: 'input:not([type]), input[type=text], input[type=email], input[type=tel], input[type=url], textarea, [role=textbox]',
		searchbox: 'input[type=search], [role=searchbox]',
		tab: '[role=tab]',
		menuitem: '[role=menuitem]',
		option: '[role=option]',
	}
	const FIELDS = ['textbox', 'searchbox', 'checkbox', 'radio']
	// The elements inside a link or a button whose text is its name as it reads (no pictures, no
	// fields, nothing hidden or named otherwise).
	const INLINE = new Set(['SPAN', 'B', 'I', 'EM', 'STRONG', 'SMALL', 'U', 'S', 'MARK', 'ABBR', 'CODE', 'SUB', 'SUP'])

	function roleOf(el) {
		const explicit = (el.getAttribute('role') || '').trim().split(/\s+/)[0]
		if (explicit) return ROLES[explicit] ? explicit : null
		// A field with suggestions is a combobox.
		if (el.hasAttribute('list')) return null
		return Object.keys(ROLES).find((role) => el.matches(ROLES[role])) || null
	}

	// Content CSS adds (an icon font's ::before) is part of the name too.
	function generatedContent(el) {
		return ['::before', '::after'].some((pseudo) => !['none', 'normal'].includes(getComputedStyle(el, pseudo).content))
	}

	function plain(el) {
		if (generatedContent(el)) return false
		return [...el.querySelectorAll('*')].every((inner) => {
			if (!INLINE.has(inner.tagName) || inner.hasAttribute('aria-label') || inner.hasAttribute('aria-hidden')) return false
			const style = getComputedStyle(inner)
			return style.display !== 'none' && style.visibility !== 'hidden' && !generatedContent(inner)
		})
	}

	// The element's accessible name where it is certain (null where it is not): its aria-label,
	// a field's one label (else its placeholder or title, when it has only one of them), a button
	// input's value, a link's or a button's plain text.
	function nameOf(el, role) {
		if (el.hasAttribute('aria-labelledby')) return null
		const label = normal(el.getAttribute('aria-label'))
		if (label) return label
		if (FIELDS.includes(role)) {
			const labels = el.labels ? [...el.labels] : []
			if (labels.length) return labels.length === 1 && !labels[0].contains(el) && plain(labels[0]) ? normal(labels[0].textContent) || null : null
			const hints = ['placeholder', 'title'].map((attribute) => normal(el.getAttribute(attribute))).filter(Boolean)
			return hints.length === 1 ? hints[0] : null
		}
		if (el.tagName === 'INPUT') return normal(el.getAttribute('value')) || null
		return plain(el) ? normal(el.textContent) || null : null
	}

	// Whether the element may have the name: its certain one, or, where that is unknown, any of
	// the texts it could come from.
	function mayBeNamed(el, role, name) {
		const certain = nameOf(el, role)
		if (certain !== null) return certain === name
		const texts = [el.getAttribute('aria-label'), el.textContent, el.value, el.getAttribute('placeholder'), el.getAttribute('title')]
		return el.hasAttribute('aria-labelledby') || (el.labels && el.labels.length > 0) || texts.some((text) => normal(text) === name)
	}

	// The element whose own text is the text: no element inside it has it all (text-is finds the
	// smallest).
	function ownText(el, text) {
		return normal(el.textContent) === text && ![...el.children].some((child) => normal(child.textContent) === text)
	}

	const ROLE_SELECTOR = /^internal:role=(\w+)\[name=("(?:[^"\\]|\\.)*")s\]$/
	const TEXT_SELECTOR = /^([a-zA-Z][\w-]*):text-is\(("(?:[^"\\]|\\.)*")\)$/

	function queryPart(part, root) {
		let match = ROLE_SELECTOR.exec(part)
		if (match) {
			const role = match[1]
			const name = JSON.parse(match[2])
			if (!ROLES[role]) return []
			return [...root.querySelectorAll(ROLES[role])].filter((node) => roleOf(node) === role && mayBeNamed(node, role, name))
		}
		match = TEXT_SELECTOR.exec(part)
		if (match) {
			const text = JSON.parse(match[2])
			return [...root.querySelectorAll(match[1])].filter((node) => ownText(node, text))
		}
		try {
			return [...root.querySelectorAll(part)]
		} catch (e) {
			return []
		}
	}

	// The elements a selector finds: CSS, the role and text selectors above, after a scope (>>).
	function query(selector, root = document) {
		let roots = [root]
		for (const part of selector.split(' >> ')) {
			const found = new Set()
			for (const base of roots) queryPart(part.trim(), base).forEach((node) => found.add(node))
			roots = [...found]
		}
		return roots
	}

	function count(selector) {
		return selector ? query(selector).length : 0
	}

	const finds = (selector, el) => {
		const found = query(selector)
		return found.length === 1 && found[0] === el
	}

	// The element itself, by what identifies it best, without its position.
	function own(el) {
		const tag = el.tagName.toLowerCase()
		if (el.id && !generated(el.id)) return `#${css(el.id)}`
		// A next page's link that says so (rel=next) stays one on every page.
		if (/(^|\s)next(\s|$)/i.test(el.getAttribute('rel') || '')) return `${tag}[rel~="next"]`
		for (const attribute of ['data-testid', 'data-test', 'name', 'aria-label']) {
			const value = el.getAttribute(attribute)
			if (value) return `${tag}[${attribute}="${css(value)}"]`
		}
		if (tag === 'input' && el.type) return `input[type="${css(el.type)}"]`
		return tag + classes(el).map((name) => `.${css(name)}`).join('')
	}

	function nth(el) {
		let index = 1
		for (let sibling = el.previousElementSibling; sibling; sibling = sibling.previousElementSibling) {
			if (sibling.tagName === el.tagName) index++
		}
		return `:nth-of-type(${index})`
	}

	// A selector that matches only this element: its own part, then its ancestors' as needed.
	function unique(el) {
		const parts = []
		for (let node = el; node && node.nodeType === 1 && node !== document.documentElement; node = node.parentElement) {
			parts.unshift(own(node))
			let selector = parts.join(' > ')
			if (count(selector) === 1) return selector
			parts[0] += nth(node)
			selector = parts.join(' > ')
			if (count(selector) === 1) return selector
			if (parts[0].startsWith('#')) break
		}
		return parts.join(' > ')
	}

	// The selectors that find this element alone, best first (see the top): for the next page's
	// link a rel=next first, and its place among its siblings only when nothing else finds it
	// (on page 2 the first link of a pager may be "‹ previous").
	function candidates(el, next = false) {
		const tag = el.tagName.toLowerCase()
		const tries = []
		if (next && /(^|\s)next(\s|$)/i.test(el.getAttribute('rel') || '')) tries.push(`${tag}[rel~="next"]`)
		for (const attribute of TEST_IDS) {
			const value = el.getAttribute(attribute)
			if (value && !generated(value)) tries.push(`[${attribute}="${css(value)}"]`)
		}
		if (el.id && !generated(el.id)) tries.push(`#${css(el.id)}`)
		const role = roleOf(el)
		const name = role && nameOf(el, role)
		const named = name && name.length <= 80 ? `internal:role=${role}[name=${JSON.stringify(name)}s]` : ''
		if (named) tries.push(named)
		for (const attribute of ['aria-label', 'name']) {
			const value = el.getAttribute(attribute)
			if (value && !generated(value)) tries.push(`${tag}[${attribute}="${css(value)}"]`)
		}
		const kept = classes(el)
		if (kept.length) tries.push(tag + kept.map((name) => `.${css(name)}`).join(''))
		const text = normal(el.textContent)
		if (!named && text && text.length <= 80 && plain(el)) tries.push(`${tag}:text-is(${JSON.stringify(text)})`)
		const found = []
		for (const selector of tries) {
			const scoped = finds(selector, el) ? selector : within(selector, el)
			if (scoped && !found.includes(scoped)) found.push(scoped)
		}
		const structural = unique(el)
		const positional = /:nth-of-type/.test(structural)
		if (!found.includes(structural) && finds(structural, el) && !(next && positional && found.length)) found.push(structural)
		return found.slice(0, MOST)
	}
	state.candidates = candidates

	// The selector within the nearest ancestor with a test id or a stable id, when it finds the
	// element alone there (a field's name in one form of two, say).
	function within(selector, el) {
		for (let node = el.parentElement; node && node !== document.body; node = node.parentElement) {
			const id = TEST_IDS.map((attribute) => [attribute, node.getAttribute(attribute)]).find(([, value]) => value && !generated(value))
			const scope = id ? `[${id[0]}="${css(id[1])}"]` : node.id && !generated(node.id) ? `#${css(node.id)}` : ''
			if (!scope) continue
			const found = query(selector, node)
			if (count(scope) !== 1 || found.length !== 1 || found[0] !== el) return ''
			return selector.startsWith('internal:') || selector.includes(':text-is(') ? `${scope} >> ${selector}` : `${scope} ${selector}`
		}
		return ''
	}

	// A step's element: its best selector and the alternatives.
	function located(el) {
		const found = candidates(el)
		return { selector: found[0] || unique(el), alternatives: found.slice(1) }
	}

	// Other selectors that match exactly the same links as the picked ones (selector): the link's
	// own part within the nearest stable ancestor, or alone, and the links' common file type.
	function similarAlternatives(el, selector) {
		const picked = new Set(query(selector))
		const same = (other) => {
			const found = query(other)
			return found.length === picked.size && found.every((node) => picked.has(node))
		}
		const tag = el.tagName.toLowerCase()
		const part = tag + classes(el).map((name) => `.${css(name)}`).join('')
		const tries = []
		for (let node = el.parentElement; node && node !== document.body; node = node.parentElement) {
			const id = TEST_IDS.map((attribute) => [attribute, node.getAttribute(attribute)]).find(([, value]) => value && !generated(value))
			const scope = id ? `[${id[0]}="${css(id[1])}"]` : node.id && !generated(node.id) ? `#${css(node.id)}` : ''
			if (scope) {
				tries.push(`${scope} ${part}`, `${scope} ${tag}`)
				break
			}
		}
		tries.push(part)
		const type = /\.([a-z0-9]{2,5})$/i.exec((el.getAttribute('href') || '').split(/[?#]/)[0])
		if (type) tries.push(`${tag}[href$=".${type[1]}" i]`)
		return [...new Set(tries)].filter((other) => other !== selector && same(other)).slice(0, MOST - 1)
	}

	// A selector for this element and those like it: the shortest chain of its ancestors' stable
	// parts (no positions) whose matches do not change when one more ancestor is added.
	function similar(el) {
		const chain = []
		for (let node = el; node && node.nodeType === 1 && node !== document.body; node = node.parentElement) {
			chain.unshift(own(node).startsWith('#') && node !== el ? `#${css(node.id)}` : own(node))
		}
		const matches = []
		for (let depth = 1; depth <= Math.min(chain.length, 8); depth++) {
			const selector = chain.slice(-depth).join(' ')
			const found = count(selector)
			matches.push({ selector, found })
		}
		for (let i = 0; i < matches.length - 1; i++) {
			if (matches[i].found >= 2 && matches[i].found === matches[i + 1].found) return matches[i].selector
		}
		const plural = matches.filter((m) => m.found >= 2)
		return plural.length ? plural[plural.length - 1].selector : unique(el)
	}

	function highlight(selector, pattern, hits) {
		for (const el of state.highlighted) {
			el.style.outline = el.dataset.harvesterOutline || ''
			el.style.outlineOffset = ''
		}
		state.highlighted = []
		if (!selector || !count(selector)) return { found: 0, matching: 0 }
		let regex = null
		try {
			// Python's pattern as JavaScript reads it (patterns.js_regex), or, from an older
			// recorder, as it was: Python's named groups are (?P<name>...), JavaScript's (?<name>...).
			if (pattern && typeof pattern === 'object') regex = new RegExp(pattern.source, pattern.flags)
			else if (pattern) regex = new RegExp(pattern.replace(/\(\?P</g, '(?<'))
		} catch (e) {}
		let matching = 0
		for (const [index, el] of query(selector).entries()) {
			// Without a pattern JavaScript reads, Python's matches (one per element, in order).
			const hit = regex ? regex.test(el.href || '') : Array.isArray(hits) ? Boolean(hits[index]) : true
			matching += hit ? 1 : 0
			el.dataset.harvesterOutline = el.style.outline
			el.style.outline = hit ? '3px solid #0082c9' : '2px dashed #999'
			el.style.outlineOffset = '2px'
			state.highlighted.push(el)
		}
		return { found: state.highlighted.length, matching }
	}
	state.highlight = highlight

	// The selector of a frame's element (an iframe) in this document, for a step to find the frame
	// again: by what names it, else by its address without the query (which may change from visit
	// to visit), else by its place.
	function frameSelector(el) {
		const tag = el.tagName.toLowerCase()
		const tries = []
		if (el.id && !/\d{3,}/.test(el.id)) tries.push(`#${css(el.id)}`)
		for (const attribute of ['name', 'title', 'data-testid', 'aria-label']) {
			const value = el.getAttribute(attribute)
			if (value && !/\d{3,}/.test(value)) tries.push(`${tag}[${attribute}="${css(value)}"]`)
		}
		const src = (el.getAttribute('src') || '').split(/[?#]/)[0]
		if (src && !src.startsWith('data:')) tries.push(`${tag}[src^="${css(src)}"]`)
		return tries.find((selector) => count(selector) === 1) || unique(el)
	}
	state.frameSelector = frameSelector

	function link(el) {
		return el.closest('a[href], [onclick], button') || el
	}

	// What a click picks: a link or a control around the element, else the outermost element
	// with the hand cursor around it (a row or card a script opens shows one), else the element.
	const PICKABLE = 'a, button, [onclick], [role=button], [role=link], input[type=button], input[type=submit], summary'
	function pickable(el) {
		const found = el.closest(PICKABLE)
		if (found) return found
		const pointer = (node) => node && node.nodeType === 1 && getComputedStyle(node).cursor === 'pointer'
		if (!pointer(el)) return el
		let top = el
		while (top.parentElement && top.parentElement !== document.body && pointer(top.parentElement)) top = top.parentElement
		return top
	}

	// An element's address a job can open: an http(s) link's that goes elsewhere than this page
	// (not href="#", not javascript:), else '' (an element the job clicks).
	function addressOf(el) {
		const href = typeof el.href === 'string' ? el.href : ''
		if (!/^https?:/i.test(href) || (el.getAttribute('href') || '').trim().startsWith('#')) return ''
		return href
	}

	// An element's text as the job tells it by: its text, a button's value, else its label (an
	// image's alt).
	function labelOf(el) {
		return (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('alt') || '').trim()
	}

	// An image's attributes (an <img>, a <picture>: its own and its sources'), from which the
	// recorder takes its address at full size (harvester.images, which reads them alike); null for
	// another element.
	function imageInfo(e) {
		const tag = (e.tagName || '').toLowerCase()
		const img = tag === 'img' ? e : tag === 'picture' ? e.querySelector('img') : null
		if (!img && tag !== 'picture') return null
		const parent = img && img.parentElement
		const box = tag === 'picture' ? e : parent && parent.tagName.toLowerCase() === 'picture' ? parent : null
		const attrs = (el) => el ? Object.fromEntries([...el.attributes].map((a) => [a.name, a.value])) : {}
		return { img: attrs(img), sources: box ? [...box.querySelectorAll('source')].map(attrs) : [], base: document.baseURI }
	}

	// The elements of the container whose own text is a number (1 2 3, not "Page 2 of 9").
	function numbersIn(box) {
		return [...box.querySelectorAll('*')].filter((node) => {
			const text = (node.textContent || '').trim()
			// The number's own element: none inside it holds a number (a box of 1 and 2 reads "12").
			return /^\d{1,6}$/.test(text) && ![...node.querySelectorAll('*')].some((inner) => /^\d/.test((inner.textContent || '').trim()))
		})
	}

	// A number picked as the next page's link: the nearest ancestor holding other numbers too,
	// the pagination (its selector for the recorder to find the current page and the next in).
	function pagination(el) {
		if (!/^\d{1,6}$/.test((el.textContent || '').trim())) return null
		for (let box = el.parentElement; box && box !== document.body; box = box.parentElement) {
			if (numbersIn(box).length >= 2) return { selector: unique(box) }
		}
		return null
	}

	// A frame (or a page) that starts after the recorder set the mode asks for it: the recorder sets
	// it in the frames there are at the time only.
	Promise.resolve(report({ action: 'mode' }))
		.then((mode) => { if (typeof mode === 'string') state.mode = mode })
		.catch(() => {})

	// The mouse came into this document (from outside it: another frame, the parent), or the focus
	// did: the person acts here now.
	document.addEventListener('mouseover', (event) => {
		const from = event.relatedTarget
		if (!from || /^i?frame$/i.test(from.tagName || '')) report({ action: 'where' })
	}, true)
	document.addEventListener('focusin', () => report({ action: 'where' }), true)

	// The element an event happened at, inside an open shadow root too (an event from there is
	// retargeted to its host by the time it reaches the document; composedPath keeps it).
	function realTarget(event) {
		const path = event.composedPath ? event.composedPath() : []
		return path.length && path[0].nodeType === 1 ? path[0] : event.target
	}

	// A selector of an element in a shadow root: matching only it among the root's elements.
	function inRoot(el, root) {
		const matches = (selector) => {
			try {
				return root.querySelectorAll(selector).length
			} catch (e) {
				return 0
			}
		}
		const parts = []
		for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
			parts.unshift(own(node))
			if (matches(parts.join(' > ')) === 1) return parts.join(' > ')
			parts[0] += nth(node)
			if (matches(parts.join(' > ')) === 1) return parts.join(' > ')
		}
		return parts.join(' > ')
	}

	// The element's selector for a step. Inside an open shadow root it is its host's, a space and
	// its own in the root: Playwright's CSS goes down into open shadow roots at a space (a closed
	// one it cannot enter, nor can this script).
	function selectorOf(el) {
		const root = el.getRootNode()
		return root instanceof ShadowRoot ? `${selectorOf(root.host)} ${inRoot(el, root)}` : unique(el)
	}

	// A step's element as located() gives it (its selector and alternatives); inside a shadow root
	// its selector through the host alone (the alternatives are the document's).
	function locatedAt(el) {
		return el.getRootNode() instanceof ShadowRoot ? { selector: selectorOf(el), alternatives: [] } : located(el)
	}

	// The elements with a closed shadow root: a click inside one reaches this script as a click on
	// the element itself, which the recorder says.
	const closedHosts = new WeakSet()
	const attachShadow = Element.prototype.attachShadow
	Element.prototype.attachShadow = function (init) {
		if (init && init.mode === 'closed') closedHosts.add(this)
		return attachShadow.call(this, init)
	}

	// The fields that are or were a password's: type password, or a browser's autocomplete for
	// one, seen at any time (on focus, on input, as an attribute changes). A "show password"
	// switch makes the field text while one types: its value is still never reported.
	const secret = new WeakSet()
	const PASSWORD_COMPLETE = /(^|\s)(current-password|new-password)(\s|$)/i
	function noteSecret(el) {
		if (el && el.nodeType === 1 && el.tagName === 'INPUT'
			&& (el.type === 'password' || PASSWORD_COMPLETE.test(el.getAttribute('autocomplete') || ''))) secret.add(el)
		return Boolean(el) && secret.has(el)
	}
	new MutationObserver((records) => {
		for (const record of records) {
			if (record.attributeName === 'type' && record.oldValue === 'password') secret.add(record.target)
			noteSecret(record.target)
		}
	}).observe(document, { subtree: true, attributes: true, attributeFilter: ['type', 'autocomplete'], attributeOldValue: true })
	for (const kind of ['focusin', 'input', 'keydown']) {
		document.addEventListener(kind, (event) => { noteSecret(realTarget(event)) }, true)
	}

	// A one-time code's field (an authenticator's code): the job fills in the current code. By its
	// name or autocomplete for sure; a field named code, otc (Microsoft's) or challenge
	// (Nextcloud's), or one for 6 to 8 digits, only after a password (recipe: _fill).
	const OTP_NAME = /otp|totp|2fa|mfa|one.?time|verification|security.?code|auth.?code|token/i
	const CODE_NAME = /^(code|otc|challenge)$|(^|[-_])otc([-_]|$)/i
	function codeKind(el, value) {
		if (!/^\d{6,10}$/.test(value.trim())) return {}
		const named = [el.name, el.id, el.placeholder, el.getAttribute('aria-label') || ''].join(' ')
		if (el.autocomplete === 'one-time-code' || OTP_NAME.test(named)) return { otp: true }
		const digits = el.inputMode === 'numeric' && el.maxLength >= 6 && el.maxLength <= 8
		return [el.name, el.id].some((name) => CODE_NAME.test(name || '')) || digits ? { code_like: true } : {}
	}

	// What a field holds: its value, or the text of an element edited in place (contenteditable).
	const valueOf = (el) => ('value' in el && !el.isContentEditable ? String(el.value) : el.innerText || '')

	// The value last reported per field: a field reported as Enter submits it (its change comes
	// after) is not reported again with the same value.
	const reported = new WeakMap()
	function reportFill(el) {
		const value = valueOf(el)
		if (reported.get(el) === value) return
		reported.set(el, value)
		const hidden = noteSecret(el)
		report({
			action: 'fill',
			...locatedAt(el),
			// A password is never reported: the job fills in the account's.
			value: hidden ? '' : value,
			...(hidden ? {} : codeKind(el, value)),
			field: hidden ? 'password' : el.isContentEditable ? 'contenteditable' : el.type,
			form: el.form ? selectorOf(el.form) : '',
		})
	}

	// Elements hidden as the mouse came into an element, and that element: a script that opens a
	// menu under the mouse shows them after this (capturing) listener has seen them hidden. Not
	// for more than a few hundred elements (a page's whole body).
	const shownBy = new WeakMap()
	const CLICKABLE = 'a[href], button, [role=menuitem], [role=option], [role=tab], [onclick], input[type=submit], input[type=button]'
	const invisible = (el) => !el.getClientRects().length || getComputedStyle(el).visibility === 'hidden'
	document.addEventListener('mouseover', (event) => {
		if (state.mode !== 'record') return
		const from = event.relatedTarget
		for (let node = realTarget(event); node && node.nodeType === 1 && node !== document.body; node = node.parentElement) {
			if (from && from.nodeType === 1 && node.contains(from)) break
			const inside = node.querySelectorAll(CLICKABLE)
			if (inside.length > 300) break
			for (const candidate of inside) {
				if (invisible(candidate)) shownBy.set(candidate, node)
			}
		}
	}, true)

	// A style sheet's rule that shows the element (or an ancestor) while another of its ancestors
	// is under the mouse (.menu:hover .sub { display: block }): that ancestor. Sheets of other
	// origins cannot be read and are passed over.
	const SHOWING = ['display', 'visibility', 'opacity', 'max-height', 'height', 'transform', 'left', 'top', 'clip', 'clip-path']
	function cssHover(el) {
		for (const sheet of document.styleSheets) {
			let rules = []
			try {
				rules = sheet.cssRules
			} catch (e) {
				continue
			}
			for (const rule of rules) {
				if (!rule.selectorText || !rule.selectorText.includes(':hover') || !SHOWING.some((name) => rule.style.getPropertyValue(name))) continue
				for (const selector of rule.selectorText.split(',')) {
					const at = selector.indexOf(':hover')
					const hovered = selector.slice(0, at).trim()
					if (at < 0 || !hovered) continue
					try {
						const shown = el.closest(selector.replace(/:hover/g, ''))
						const over = shown && shown.parentElement && shown.parentElement.closest(hovered)
						if (over) return over
					} catch (e) {}
				}
			}
		}
		return null
	}

	// The element whose hover showed the one clicked (a menu's): it holds the element, so the mouse
	// is over it as the click comes. (Chromium's :hover does not say so of an element's ancestors
	// at that moment.)
	function hoverOf(el) {
		const over = shownBy.get(el)
		const found = over && over !== el && over.contains(el) ? over : cssHover(el)
		return found && found !== el ? selectorOf(found) : ''
	}

	let hovered = null
	const TYPING = 'textarea, select, input:not([type=submit]):not([type=button]):not([type=checkbox]):not([type=radio]):not([type=image]):not([type=reset])'
	const SUBMIT = 'button:not([type]), button[type=submit], input[type=submit], input[type=image]'
	const picking = () => ['follow', 'links', 'next', 'value', 'tags', 'page-value', 'page-tags', 'until'].includes(state.mode)
	// The modes that pick an element itself (a text, a tag), not a link or a control around it.
	const ELEMENT_MODES = ['value', 'tags', 'page-value', 'page-tags', 'until']

	// An element by its tag and stable classes: alike in every row or card, unlike an id.
	function generic(el) {
		return el.tagName.toLowerCase() + classes(el).map((name) => `.${css(name)}`).join('')
	}

	// A selector for el within box (box.querySelector finds el): the shortest chain of generic
	// parts, else the path from box down with positions.
	function inBox(box, el) {
		const parts = []
		for (let node = el; node && node !== box; node = node.parentElement) {
			parts.unshift(generic(node))
			try {
				if (box.querySelector(parts.join(' > ')) === el) return parts.join(' > ')
			} catch (e) {}
		}
		const placed = []
		for (let node = el; node && node !== box; node = node.parentElement) placed.unshift(generic(node) + nth(node))
		return `:scope > ${placed.join(' > ')}`
	}

	// Where a picked value is from its link: in the link, or in the row or card (the nearest
	// ancestor holding one of the links alone); null when it is near none of them.
	function valuePick(el) {
		let links = []
		try {
			links = state.valueLinks ? [...document.querySelectorAll(state.valueLinks)] : []
		} catch (e) {}
		const text = (el.innerText || el.textContent || '').trim().slice(0, 200)
		// A <time> holds the date a script can read in its datetime, whatever it shows.
		const attribute = el.matches('time[datetime]') ? 'datetime' : ''
		const own = links.find((link) => link === el || link.contains(el))
		if (own) return { closest: '', selector: own === el ? '' : inBox(own, el), attribute, text }
		for (let box = el.parentElement; box && box !== document.body; box = box.parentElement) {
			const inside = links.filter((link) => box.contains(link))
			if (inside.length > 1) break
			if (inside.length === 1) {
				const closest = [generic(box), box.tagName.toLowerCase()].find((selector) => inside[0].closest(selector) === box) || generic(box)
				return { closest, selector: inBox(box, el), attribute, text }
			}
		}
		return null
	}

	// Tags picked near a level's links (a chip in a link's card): the card as valuePick finds it,
	// and a selector in it for the chip and its siblings alike (its parent's part and its own),
	// outlined in every card; null when it is near none of the links.
	function tagsPick(el) {
		const found = valuePick(el)
		if (!found || !found.closest) return null
		let box = el.parentElement
		while (box && !box.matches(found.closest)) box = box.parentElement
		if (!box) return null
		const parent = el.parentElement
		const selector = parent && parent !== box ? `${generic(parent)} > ${generic(el)}` : generic(el)
		highlight(`${found.closest} ${selector}`, '')
		return { closest: found.closest, selector, attribute: '', text: found.text }
	}

	// A plain CSS selector that document.querySelector finds the element by (Playwright's role
	// and text selectors are no CSS: a job reads the page with them, BeautifulSoup does not).
	function plainSelector(el) {
		const shown = (selector) => { try { return document.querySelector(selector) === el } catch (e) { return false } }
		return candidates(el).find(shown) || unique(el)
	}

	// Tags picked on a page a level's links open: the chip and those like it (as similar links are
	// picked), outlined, their selector plain CSS.
	function pageTagsPick(el) {
		let selector = similar(el)
		try { document.querySelectorAll(selector) } catch (e) { selector = generic(el) }
		highlight(selector, '')
		return { selector, texts: query(selector).slice(0, 50).map((node) => labelOf(node).slice(0, 80)) }
	}

	// The cursor the page shows where the mouse is (a hand over a link, a bar in a field), or a
	// crosshair while picking: the recorder's screen shows it, as a browser would.
	let cursor = ''
	document.addEventListener('mouseover', (event) => {
		let wanted = picking() ? 'crosshair' : getComputedStyle(event.target).cursor
		if (wanted === 'auto') wanted = event.target.matches(TYPING) || event.target.isContentEditable ? 'text' : 'default'
		if (wanted !== cursor) {
			cursor = wanted
			report({ action: 'cursor', cursor })
		}
	}, true)

	document.addEventListener('mouseover', (event) => {
		if (!picking()) {
			if (hovered) hovered.style.boxShadow = ''
			return
		}
		if (hovered) hovered.style.boxShadow = ''
		hovered = ELEMENT_MODES.includes(state.mode) ? event.target : state.mode === 'next' ? link(event.target) : pickable(event.target)
		hovered.style.boxShadow = 'inset 0 0 0 9999px rgba(0, 130, 201, 0.15)'
	}, true)

	// The control a label's click goes on to (the browser clicks it too: one click of the person,
	// one step), and the form Enter submitted (the browser clicks its submit button, detail 0).
	let forwarded = null
	let submitted = null

	// A click while recording: a step, unless it only focuses a field (what is typed there is the
	// step) or the browser made it (above). A file to upload cannot be recorded: a job has none.
	function recordClick(event) {
		const target = realTarget(event)
		if (target === forwarded) {
			forwarded = null
			return
		}
		if (event.detail === 0 && submitted && Date.now() - submitted.at < 1000 && target.matches(SUBMIT) && target.form === submitted.form) {
			submitted = null
			return
		}
		const label = target.closest('label')
		const control = target.matches('input[type=file]') ? target : label && label.control
		if (control && control.matches('input[type=file]')) {
			event.preventDefault()
			report({ action: 'refused', what: 'file' })
			return
		}
		if (label && label.control && label.control !== target) {
			forwarded = label.control
			setTimeout(() => { forwarded = null }, 0)
		}
		if (closedHosts.has(target)) report({ action: 'refused', what: 'closed-shadow' })
		if (target.matches(TYPING) || target.isContentEditable) return
		const el = link(target)
		report({ action: 'click', ...locatedAt(el), text: (el.innerText || el.value || '').trim().slice(0, 80), hover: hoverOf(el) })
	}

	document.addEventListener('click', (event) => {
		// A cookie banner answered on its own (consent.js) is neither a step nor a pick.
		if (document.documentElement.hasAttribute('data-harvester-consent')) return
		if (state.mode === 'record') {
			recordClick(event)
			return
		}
		const el = state.mode === 'next' ? link(event.target) : pickable(event.target)
		if (!picking()) return
		event.preventDefault()
		event.stopImmediatePropagation()
		if (state.mode === 'until') {
			// Plain CSS only (no role or text selector of Playwright's): the first that finds it.
			const shown = (selector) => { try { return document.querySelector(selector) === event.target } catch (e) { return false } }
			report({ action: 'pick', target: 'until', selector: candidates(event.target).find(shown) || unique(event.target) })
			return
		}
		if (state.mode === 'value') {
			const found = valuePick(event.target)
			report(found ? { action: 'pick', target: 'value', ...found } : { action: 'pick', target: 'value', none: true })
			return
		}
		if (state.mode === 'tags') {
			const found = tagsPick(event.target)
			report(found ? { action: 'pick', target: 'tags', ...found } : { action: 'pick', target: 'tags', none: true })
			return
		}
		if (state.mode === 'page-value') {
			const target = event.target
			const attribute = target.matches('time[datetime]') ? 'datetime' : ''
			report({ action: 'pick', target: 'page-value', selector: plainSelector(target), attribute, text: (target.innerText || target.textContent || '').trim().slice(0, 200) })
			return
		}
		if (state.mode === 'page-tags') {
			report({ action: 'pick', target: 'page-tags', ...pageTagsPick(event.target) })
			return
		}
		const next = state.mode === 'next' ? candidates(el, true) : []
		const selector = state.mode === 'next' ? next[0] || unique(el) : similar(el)
		const alternatives = state.mode === 'next' ? next.slice(1) : similarAlternatives(el, selector)
		const matches = query(selector).slice(0, 300).map((match) => [addressOf(match), labelOf(match).slice(0, 80), imageInfo(match)])
		if (state.mode === 'next') highlight(selector, '')
		const numbered = state.mode === 'next' ? pagination(el) : null
		report({ action: 'pick', target: state.mode, selector, alternatives, found: count(selector), matches, numbered })
	}, true)

	// A field's new value: a list's choice (a step "select", by its value and the text it shows),
	// else the field filled. Checkboxes and radio buttons are their clicks.
	function changed(event) {
		const el = event.target
		if (state.mode !== 'record' || !el.matches('input, select, textarea')) return
		if (['checkbox', 'radio'].includes(el.type)) return
		if (el.matches('input[type=file]')) return
		if (el.tagName === 'SELECT') {
			const chosen = [...el.selectedOptions]
			report({
				action: 'select',
				...locatedAt(el),
				value: el.multiple ? chosen.map((option) => option.value) : el.value,
				label: chosen.length === 1 ? chosen[0].label.trim() : '',
			})
			return
		}
		reportFill(el)
	}
	document.addEventListener('change', changed, true)

	// A change inside a shadow root stays there (the event does not leave it): each open root
	// the focus goes into gets the listener too. Text edited in place has no change: it is
	// reported as the focus leaves it.
	const listening = new WeakSet()
	const editing = new WeakMap()
	document.addEventListener('focusin', (event) => {
		const el = realTarget(event)
		const root = el.getRootNode()
		if (root instanceof ShadowRoot && !listening.has(root)) {
			listening.add(root)
			root.addEventListener('change', changed, true)
		}
		if (el.isContentEditable) editing.set(el, el.innerText)
	}, true)
	document.addEventListener('focusout', (event) => {
		const el = realTarget(event)
		if (state.mode === 'record' && el.isContentEditable && editing.has(el) && editing.get(el) !== el.innerText) reportFill(el)
	}, true)

	// Keys that are steps: Enter in a field (it submits), and the keys that work a list of
	// suggestions (an autocomplete, a combobox): the arrows, Escape, Tab and Enter. Typing is the
	// fill; any other key is not recorded. The field's value goes first: the key may submit or
	// change it before its change comes.
	const COMBOBOX = '[role=combobox], [aria-autocomplete], input[list], [aria-haspopup=listbox]'
	const LIST_KEYS = ['ArrowUp', 'ArrowDown', 'Escape', 'Tab', 'Enter']
	document.addEventListener('keydown', (event) => {
		const el = realTarget(event)
		if (state.mode !== 'record' || !LIST_KEYS.includes(event.key)) return
		const combobox = Boolean(el.closest(COMBOBOX))
		const enter = event.key === 'Enter' && el.matches('input')
		if (!combobox && !enter) return
		if (el.matches('input, textarea') || el.isContentEditable) reportFill(el)
		if (event.key === 'Enter' && el.form) submitted = { form: el.form, at: Date.now() }
		report({ action: 'press', ...locatedAt(el), key: event.key })
	}, true)
})()
