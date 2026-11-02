// Runs in every page of the recorder's browser (an init script). In the mode record it turns
// what the person does into steps (click, fill, press Enter); in browse it lets them go
// anywhere unrecorded. In a picking mode the element clicked becomes a selector: for links to
// follow and file links one that matches the similar links too (with all of them, for the
// recorder to propose a pattern), for the next page's link one that matches only it (and, when
// it is a number among numbers, 1 2 3 …, the numbers' container: numbered pages).
// highlight(selector, pattern) outlines the selector's links: solid where the pattern matches
// their URL, dashed where it does not. Everything goes to the recorder through
// window.__harvesterReport.
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

	function classes(el) {
		return [...el.classList].filter((name) => !unstable.test(name)).slice(0, 3)
	}

	function count(selector) {
		try {
			return document.querySelectorAll(selector).length
		} catch (e) {
			return 0
		}
	}

	// The element itself, by what identifies it best, without its position.
	function own(el) {
		const tag = el.tagName.toLowerCase()
		if (el.id && !/\d{3,}/.test(el.id)) return `#${css(el.id)}`
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

	function highlight(selector, pattern) {
		for (const el of state.highlighted) {
			el.style.outline = el.dataset.harvesterOutline || ''
			el.style.outlineOffset = ''
		}
		state.highlighted = []
		if (!selector || !count(selector)) return { found: 0, matching: 0 }
		let regex = null
		try {
			// Python's named groups are (?P<name>...), JavaScript's (?<name>...).
			regex = pattern ? new RegExp(pattern.replace(/\(\?P</g, '(?<')) : null
		} catch (e) {}
		let matching = 0
		for (const el of document.querySelectorAll(selector)) {
			const hit = !regex || regex.test(el.href || '')
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
	function within(el, root) {
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
		return root instanceof ShadowRoot ? `${selectorOf(root.host)} ${within(el, root)}` : unique(el)
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
			selector: selectorOf(el),
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
	const picking = () => ['follow', 'links', 'next'].includes(state.mode)

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
		hovered = link(event.target)
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
		report({ action: 'click', selector: selectorOf(el), text: (el.innerText || el.value || '').trim().slice(0, 80), hover: hoverOf(el) })
	}

	document.addEventListener('click', (event) => {
		if (state.mode === 'record') {
			recordClick(event)
			return
		}
		const el = link(event.target)
		if (!picking()) return
		event.preventDefault()
		event.stopImmediatePropagation()
		const selector = state.mode === 'next' ? unique(el) : similar(el)
		const matches = [...document.querySelectorAll(selector)].slice(0, 300)
			.map((match) => [match.href || '', (match.innerText || '').trim().slice(0, 80)])
		if (state.mode === 'next') highlight(selector, '')
		const numbered = state.mode === 'next' ? pagination(el) : null
		report({ action: 'pick', target: state.mode, selector, found: count(selector), matches, numbered })
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
				selector: selectorOf(el),
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
		report({ action: 'press', selector: selectorOf(el), key: event.key })
	}, true)
})()
