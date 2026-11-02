// Runs in every page of the recorder's browser (an init script). In the mode record it turns
// what the person does into steps (click, fill, press Enter); in browse it lets them go
// anywhere unrecorded. In a picking mode the element clicked becomes a selector: for links to
// follow and file links one that matches the similar links too (with all of them, for the
// recorder to propose a pattern), for the next page's link one that matches only it (and, when
// it is a number among numbers, 1 2 3 …, the numbers' container: numbered pages).
// highlight(selector, pattern) outlines the selector's links: solid where the pattern matches
// their URL, dashed where it does not. Everything goes to the recorder through
// window.__harvesterReport.
(() => {
	if (window.__harvester) return
	const state = { mode: 'record', highlighted: [] }
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

	let hovered = null
	const TYPING = 'textarea, select, input:not([type=submit]):not([type=button]):not([type=checkbox]):not([type=radio]):not([type=image]):not([type=reset])'
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

	document.addEventListener('click', (event) => {
		const el = link(event.target)
		if (state.mode === 'record') {
			// Clicking into a field only focuses it: what is typed there is the step.
			if (!event.target.matches(TYPING)) {
				report({ action: 'click', selector: unique(el), text: (el.innerText || el.value || '').trim().slice(0, 80) })
			}
			return
		}
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

	document.addEventListener('change', (event) => {
		const el = event.target
		if (state.mode !== 'record' || !el.matches('input, select, textarea')) return
		if (['checkbox', 'radio'].includes(el.type)) return
		report({
			action: 'fill',
			selector: unique(el),
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
		report({ action: 'press', selector: unique(event.target), key: 'Enter' })
	}, true)
})()
