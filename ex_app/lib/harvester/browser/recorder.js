// Runs in every page of the recorder's browser (an init script). In the mode record it turns
// what the person does into steps (click, fill, press Enter); in browse it lets them go
// anywhere unrecorded. In a picking mode the element clicked becomes a selector: for links to
// follow and file links one that matches the similar links too (with all of them, for the
// recorder to propose a pattern), for the next page's link one that matches only it (and, when
// it is a number among numbers, 1 2 3 …, the numbers' container: numbered pages).
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
// It runs in every frame too (a consent banner's, a sign-in's), cross-origin ones included: the
// recorder learns from the report itself which frame it came from, and asks the frame's parent
// for its element's selector (frameSelector). A frame reports when the mouse or the focus comes
// into it ("where"), for the recorder to show where the person acts.
(() => {
	if (window.__harvester) return
	const state = { mode: 'record', highlighted: [] }
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

	let hovered = null
	const TYPING = 'textarea, select, input:not([type=submit]):not([type=button]):not([type=checkbox]):not([type=radio]):not([type=image]):not([type=reset])'
	const picking = () => ['follow', 'links', 'next', 'value'].includes(state.mode)

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
		hovered = state.mode === 'value' ? event.target : link(event.target)
		hovered.style.boxShadow = 'inset 0 0 0 9999px rgba(0, 130, 201, 0.15)'
	}, true)

	document.addEventListener('click', (event) => {
		const el = link(event.target)
		if (state.mode === 'record') {
			// Clicking into a field only focuses it: what is typed there is the step.
			if (!event.target.matches(TYPING)) {
				report({ action: 'click', ...located(el), text: (el.innerText || el.value || '').trim().slice(0, 80) })
			}
			return
		}
		if (!picking()) return
		event.preventDefault()
		event.stopImmediatePropagation()
		if (state.mode === 'value') {
			const found = valuePick(event.target)
			report(found ? { action: 'pick', target: 'value', ...found } : { action: 'pick', target: 'value', none: true })
			return
		}
		const next = state.mode === 'next' ? candidates(el, true) : []
		const selector = state.mode === 'next' ? next[0] || unique(el) : similar(el)
		const alternatives = state.mode === 'next' ? next.slice(1) : similarAlternatives(el, selector)
		const matches = query(selector).slice(0, 300)
			.map((match) => [match.href || '', (match.innerText || '').trim().slice(0, 80)])
		if (state.mode === 'next') highlight(selector, '')
		const numbered = state.mode === 'next' ? pagination(el) : null
		report({ action: 'pick', target: state.mode, selector, alternatives, found: count(selector), matches, numbered })
	}, true)

	document.addEventListener('change', (event) => {
		const el = event.target
		if (state.mode !== 'record' || !el.matches('input, select, textarea')) return
		if (['checkbox', 'radio'].includes(el.type)) return
		report({
			action: 'fill',
			...located(el),
			// A password is never reported: the job fills in the account's.
			value: el.type === 'password' ? '' : el.value,
			// A one-time code's field (an authenticator's code): the job fills in the current code.
			otp: el.type !== 'password' && /^\d{6,10}$/.test(el.value.trim())
				&& (el.autocomplete === 'one-time-code'
					|| /otp|totp|2fa|mfa|one.?time|verification|security.?code|auth.?code|token/i.test(
						[el.name, el.id, el.placeholder, el.getAttribute('aria-label') || ''].join(' '))),
			field: el.type === 'password' ? 'password' : el.type,
			form: el.form ? unique(el.form) : '',
		})
	}, true)

	document.addEventListener('keydown', (event) => {
		if (state.mode !== 'record' || event.key !== 'Enter' || !event.target.matches('input')) return
		// The value first: Enter may submit before change fires.
		event.target.dispatchEvent(new Event('change', { bubbles: true }))
		report({ action: 'press', ...located(event.target), key: 'Enter' })
	}, true)
})()
