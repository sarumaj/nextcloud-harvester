// The app's page in Nextcloud: the user's jobs, a job's form (blank or from a template) with
// its account, and the recorder (the app's browser, shown and driven from here), which proposes
// the links' patterns and shows what they match. Plain JavaScript, no build: Nextcloud loads
// it into the page AppAPI renders for the top menu.
(function () {
	'use strict'

	const API = OC.generateUrl('/apps/app_api/proxy/harvester/api')
	// The recorder's WebSocket goes to HaRP directly (AppAPI's PHP proxy has none).
	const RECORDER = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}${OC.webroot || ''}/exapps/harvester/ws/record`
	const VIEWPORT = { width: 1280, height: 800 }
	const BLANK = { name: '', source: 'site', target: '/Harvester', schedule: '0 6 * * *', options: {}, tags: [], enabled: true }
	let sources = []
	let presets = []

	async function api(method, path, body) {
		const response = await fetch(API + path, {
			method,
			headers: { requesttoken: OC.requestToken, 'Content-Type': 'application/json' },
			body: body === undefined ? undefined : JSON.stringify(body),
		})
		const data = await response.json().catch(() => ({}))
		if (!response.ok) {
			const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join(', ') : data.detail
			throw new Error(detail || response.statusText)
		}
		return data
	}

	function el(tag, attributes = {}, ...children) {
		const node = document.createElement(tag)
		for (const [key, value] of Object.entries(attributes)) {
			if (key.startsWith('on')) node.addEventListener(key.slice(2), value)
			else if (key === 'text') node.textContent = value
			else node.setAttribute(key, value)
		}
		node.append(...children)
		return node
	}

	function field(label, input, help) {
		return el('label', { class: 'harvester-field' }, el('span', { text: label }), input,
			help ? el('small', { text: help }) : '')
	}

	// A size given in a unit (bytes, KB, MB, GB) in the largest unit it fills: 1572864 bytes is
	// "≈ 1.5 MB", 4096 MB "= 4 GB". Units of 1024, as file managers count.
	const SIZE_UNITS = ['bytes', 'KB', 'MB', 'GB', 'TB']
	function sizeHint(value, unit) {
		const number = Number(value)
		if (value === '' || !Number.isFinite(number) || number < 0) return ''
		let bytes = number * 1024 ** Math.max(0, SIZE_UNITS.indexOf(unit))
		let index = 0
		while (bytes >= 1024 && index < SIZE_UNITS.length - 1) {
			bytes /= 1024
			index += 1
		}
		const rounded = Math.round(bytes * 10) / 10
		return `${rounded === bytes ? '=' : '≈'} ${rounded.toLocaleString()} ${SIZE_UNITS[index]}`
	}
	window.__harvesterSizeHint = sizeHint

	// Each source's icon (24 × 24, drawn in the text's colour): a globe, an envelope, a feed, a
	// play button, a picture, a cloud, a tag.
	const ICONS = {
		site: [['circle', { cx: 12, cy: 12, r: 9 }], ['ellipse', { cx: 12, cy: 12, rx: 4, ry: 9 }], ['path', { d: 'M3 12h18M5 7h14M5 17h14' }]],
		mail: [['rect', { x: 3, y: 5, width: 18, height: 14, rx: 2 }], ['path', { d: 'M3 7l9 6 9-6' }]],
		feed: [['circle', { cx: 6, cy: 18, r: 1.5 }], ['path', { d: 'M5 11a8 8 0 0 1 8 8M5 5a14 14 0 0 1 14 14' }]],
		media: [['rect', { x: 3, y: 5, width: 18, height: 14, rx: 3 }], ['path', { d: 'M10 9l5 3-5 3z' }]],
		gallery: [['rect', { x: 3, y: 4, width: 18, height: 16, rx: 2 }], ['circle', { cx: 9, cy: 9, r: 1.5 }], ['path', { d: 'M4 18l5-5 4 4 3-3 4 4' }]],
		remote: [['path', { d: 'M7 18h10a4 4 0 0 0 .5-7.97A6 6 0 0 0 6.1 9.1 4.5 4.5 0 0 0 7 18z' }]],
		releases: [['path', { d: 'M3 12V4h8l10 10-8 8z' }], ['circle', { cx: 7.5, cy: 7.5, r: 1.5 }]],
	}
	function icon(source) {
		const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
		for (const [name, value] of Object.entries({ viewBox: '0 0 24 24', width: 20, height: 20, fill: 'none', stroke: 'currentColor', 'stroke-width': 1.8, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', class: 'harvester-icon', 'aria-hidden': 'true' })) {
			svg.setAttribute(name, String(value))
		}
		for (const [tag, attributes] of ICONS[source] || ICONS.site) {
			const shape = document.createElementNS('http://www.w3.org/2000/svg', tag)
			for (const [name, value] of Object.entries(attributes)) shape.setAttribute(name, String(value))
			svg.append(shape)
		}
		return svg
	}

	function when(timestamp) {
		return timestamp ? new Date(timestamp * 1000).toLocaleString() : 'never'
	}

	function describe(step) {
		if (step.action === 'goto') return `Open ${step.url}`
		if (step.action === 'click') return `Click ${step.text ? `“${step.text}”` : step.selector}`
		if (step.action === 'press') return `Press ${step.key} in ${step.selector}`
		if (step.credential) return `Fill in the account's ${step.credential} (${step.selector})`
		return `Fill in “${step.value}” (${step.selector})`
	}

	async function renderJobs(root) {
		const jobs = await api('GET', '/jobs')
		const table = el('table', { class: 'harvester-jobs' },
			el('thead', {}, el('tr', {}, ...['Job', 'Folder', 'Schedule', 'Tags', 'Last run', ''].map((h) => el('th', { text: h })))))
		const body = el('tbody', {})
		for (const job of jobs) {
			const state = job.running ? 'running' : job.last_status || 'never'
			const label = { running: 'Running', ok: 'OK', failed: 'Failed', auth: 'Sign-in refused', challenge: 'Needs you', never: 'Not run yet' }[state] || state
			body.append(el('tr', {},
				el('td', { class: 'harvester-with-icon', title: (sources.find((x) => x.name === job.source) || {}).title || job.source },
					icon(job.source), el('strong', { text: job.name }), job.enabled ? '' : el('span', { class: 'harvester-chip', text: 'paused' })),
				el('td', { class: 'harvester-muted', text: job.target }),
				el('td', {}, el('code', { text: job.schedule })),
				el('td', {}, ...job.tags.map((tag) => el('span', { class: 'harvester-chip', text: tag }))),
				el('td', {},
					el('span', { class: `harvester-status harvester-status-${state}`, text: label }),
					el('div', { class: 'harvester-muted harvester-message', text: job.last_run ? `${when(job.last_run)} · ${job.last_message || ''}` : '' })),
				el('td', { class: 'harvester-actions' },
					state === 'challenge' ? el('button', { class: 'primary', text: 'Continue in the browser', onclick: () => openSession(root, job) }) : '',
					el('button', { text: 'Run now', onclick: async () => { await api('POST', `/jobs/${job.id}/run`); show(root) } }),
					el('button', { text: 'Edit', onclick: () => renderForm(root, job) }),
					el('button', {
						class: 'error',
						text: 'Delete',
						onclick: async () => {
							if (confirm(`Delete ${job.name}? The files it downloaded stay.`)) {
								await api('DELETE', `/jobs/${job.id}`)
								show(root)
							}
						},
					}))))
		}
		table.append(body)
		root.replaceChildren(
			el('div', { class: 'harvester-header' },
				el('div', {},
					el('h2', { text: 'Harvester' }),
					el('p', { class: 'harvester-muted', text: 'Fetches files from websites into your folders on a schedule, and tags them.' })),
				el('button', { class: 'primary', text: 'New job', onclick: () => renderStart(root) })),
			jobs.length ? table : el('div', { class: 'harvester-empty' },
				el('p', { text: 'No jobs yet.' }),
				el('p', { class: 'harvester-muted', text: 'A job fetches the files of one site: record it in the browser, or start from a template.' })))
	}

	// A new job: blank (recorded in the browser) or from a template with its parameters.
	function renderStart(root) {
		const error = el('p', { class: 'harvester-error' })
		const cards = presets.map((preset) => {
			const inputs = {}
			const fields = Object.entries(preset.parameters).map(([name, spec]) => {
				inputs[name] = el('input', { value: spec.default })
				return field(spec.title, inputs[name], spec.description)
			})
			return el('div', { class: 'harvester-card' },
				el('h3', { class: 'harvester-with-icon' }, icon(preset.source), el('span', { text: preset.title })),
				el('p', { class: 'harvester-muted', text: preset.description }), ...fields,
				el('button', {
					class: 'primary',
					text: 'Use this template',
					onclick: async () => {
						try {
							const body = Object.fromEntries(Object.entries(inputs).map(([k, input]) => [k, input.value]))
							renderForm(root, null, await api('POST', `/presets/${preset.id}`, body))
						} catch (e) {
							error.textContent = e.message
						}
					},
				}))
		})
		// One card per source: a blank job of it.
		const kinds = sources.map((source) => el('div', { class: 'harvester-card harvester-kind' },
			el('h3', { class: 'harvester-with-icon' }, icon(source.name), el('span', { text: source.title })),
			el('p', { class: 'harvester-muted', text: source.description }),
			el('button', { class: 'primary', text: 'Start', onclick: () => renderForm(root, null, { ...BLANK, source: source.name }) })))
		root.replaceChildren(
			el('div', { class: 'harvester-header' },
				el('div', {}, el('h2', { text: 'New job' }), el('p', { class: 'harvester-muted', text: 'What should the job fetch?' })),
				el('button', { text: 'Back to the jobs', onclick: () => show(root) })),
			el('div', { class: 'harvester-kinds' }, ...kinds),
			cards.length ? el('h3', { text: 'Templates for known sites' }) : '',
			...cards, error)
	}

	async function renderCredentials(container, job, source) {
		container.replaceChildren()
		if (!source.credentials.length) return
		container.append(el('h3', { text: 'The account this job signs in with' }),
			el('p', { text: 'Filled into the recipe’s username and password fields; never shown again.' }))
		if (!job) {
			container.append(el('p', { text: 'Save the job first, then set its account here.' }))
			return
		}
		const stored = await api('GET', `/jobs/${job.id}/credentials`)
		const inputs = {}
		for (const name of source.credentials) {
			// A configuration (several lines, e.g. rclone's) in a text area; passwords and tokens hidden.
			const placeholder = stored[name] ? '(stored, leave empty to keep)' : ''
			inputs[name] = name === 'config'
				? el('textarea', { rows: '8', class: 'harvester-recipe', placeholder, autocomplete: 'off', spellcheck: 'false' })
				: el('input', { type: ['password', 'token'].includes(name) ? 'password' : 'text', placeholder, autocomplete: 'off' })
			container.append(field(name, inputs[name]))
		}
		container.append(el('button', {
			text: 'Save account',
			onclick: async (event) => {
				event.preventDefault()
				const body = Object.fromEntries(Object.entries(inputs).map(([k, input]) => [k, input.value]))
				await api('PUT', `/jobs/${job.id}/credentials`, body)
				renderCredentials(container, job, source)
			},
		}))
	}

	// One picked level in the recorder's panel: its pattern (editable), what it matches on the
	// page, examples with their values, and the proposed file name and tags.
	function renderLevel(title, spec, proposal, send, target, level, suggestions) {
		const pattern = el('input', { class: 'harvester-pattern', value: spec.pattern || '' })
		pattern.addEventListener('change', () => send({ type: 'pattern', target, level, pattern: pattern.value }))
		const box = el('div', { class: 'harvester-level' }, el('h4', { text: title }),
			el('small', { text: spec.selector }), field('Pattern (named groups become values)', pattern))
		if (!proposal) return box
		if (proposal.error) box.append(el('p', { class: 'harvester-error', text: proposal.error }))
		box.append(el('p', { text: `On this page: ${proposal.matching} of ${proposal.found} links match (solid outline; dashed: left out).` }))
		box.append(el('ul', {}, ...(proposal.examples || []).map((example) => el('li', {},
			el('span', { text: example.text || example.url }), ' ',
			...Object.entries(example.values).map(([k, v]) => el('span', { class: 'harvester-value', text: `${k}=${v}` }))))))
		if (target === 'links' && proposal.path) {
			const use = (label, apply) => el('button', { type: 'button', text: label, onclick: (e) => { apply(); e.target.textContent = '✔ will be used' } })
			box.append(el('p', { text: `Proposed file name: ${proposal.path}` }), use('Use it', () => { suggestions.path = proposal.path }))
			if (proposal.tags.length) {
				box.append(el('p', { text: `Proposed tags: ${proposal.tags.join(', ')}` }), use('Use them', () => { suggestions.tags = proposal.tags }))
			}
		}
		return box
	}

	// Keys the remote browser gets as key presses: they move, delete or confirm rather than type.
	const NON_TEXT_KEYS = new Set(['Enter', 'Tab', 'Backspace', 'Delete', 'Escape', 'ArrowLeft', 'ArrowRight', 'ArrowUp',
		'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown', 'Insert', ...Array.from({ length: 12 }, (_, i) => `F${i + 1}`)])

	// What a key press on the person's keyboard sends: a key press for the keys above and for
	// shortcuts (Ctrl or Cmd with a letter: Ctrl+A), null for everything that types, which the
	// browser turns into text with the person's own layout (AltGr, dead keys, input methods).
	// AltGr reports Ctrl+Alt on Windows: that is typing, not a shortcut. Ctrl+V pastes.
	function keyAction(event) {
		if (event.isComposing || ['Dead', 'Process', 'Unidentified', 'Shift', 'Control', 'Alt', 'AltGraph', 'Meta'].includes(event.key)) return null
		const altGraph = event.getModifierState('AltGraph') || (event.ctrlKey && event.altKey)
		const shortcut = (event.ctrlKey || event.metaKey) && !altGraph && event.key.length === 1 && event.key.toLowerCase() !== 'v'
		if (!NON_TEXT_KEYS.has(event.key) && !shortcut) return null
		const modifiers = [['Control', event.ctrlKey && !altGraph], ['Alt', event.altKey && !altGraph], ['Meta', event.metaKey], ['Shift', event.shiftKey]]
			.filter(([, on]) => on).map(([name]) => name)
		const key = shortcut ? event.key.toLowerCase() : event.key
		return { type: 'key', key: [...modifiers, key].join('+') }
	}
	window.__harvesterKeyAction = keyAction

	// The remote browser's screen: the person's mouse and wheel go to it at the browser's
	// coordinates; the keyboard types into an invisible text field over it (the person's layout),
	// whose text goes to the browser as text. Returns the box to put on the page.
	function wireScreen(screen, send) {
		const keys = el('textarea', {
			class: 'harvester-keys', 'aria-label': 'Keyboard of the remote browser',
			autocomplete: 'off', autocapitalize: 'off', autocorrect: 'off', spellcheck: 'false',
		})
		const box = el('div', { class: 'harvester-screen-box' }, screen, keys)
		screen.focusKeys = () => keys.focus({ preventScroll: true })

		function point(event) {
			const area = screen.getBoundingClientRect()
			return { x: (event.clientX - area.left) * VIEWPORT.width / area.width, y: (event.clientY - area.top) * VIEWPORT.height / area.height }
		}
		let lastMove = 0
		screen.addEventListener('mousemove', (event) => {
			if (Date.now() - lastMove < 50) return
			lastMove = Date.now()
			send({ type: 'mouse', event: 'move', ...point(event) })
		})
		for (const kind of ['down', 'up']) {
			screen.addEventListener(`mouse${kind}`, (event) => {
				event.preventDefault()
				screen.focusKeys()
				send({ type: 'mouse', event: kind, button: event.button === 2 ? 'right' : 'left', ...point(event) })
			})
		}
		screen.addEventListener('contextmenu', (event) => event.preventDefault())
		screen.addEventListener('wheel', (event) => { event.preventDefault(); send({ type: 'wheel', dx: event.deltaX, dy: event.deltaY }) }, { passive: false })

		keys.addEventListener('keydown', (event) => {
			const action = keyAction(event)
			if (!action) return // typing: the text arrives as input
			event.preventDefault()
			send(action)
		})
		// The typed text, once composed (dead keys and input methods finish in compositionend).
		const flush = () => {
			const text = keys.value
			keys.value = ''
			if (text) send({ type: 'text', text })
		}
		keys.addEventListener('input', (event) => { if (!event.isComposing) flush() })
		keys.addEventListener('compositionend', flush)
		keys.addEventListener('paste', (event) => {
			event.preventDefault()
			send({ type: 'text', text: event.clipboardData.getData('text') })
		})
		return box
	}

	// A job that met a page asking for a person: the browser opens there with the job's session;
	// the person answers it, and "Done" keeps the session and runs the job again.
	function openSession(root, job) {
		const socket = new WebSocket(`${RECORDER}?job=${job.id}`)
		const send = (message) => socket.readyState === WebSocket.OPEN && socket.send(JSON.stringify(message))
		const screen = el('img', { class: 'harvester-screen', tabindex: '0', alt: 'The job’s browser' })
		const url = el('input', { class: 'harvester-url', readonly: '', value: job.challenge_url || '' })
		const message = el('p', { class: 'harvester-muted', text: 'Starting the browser…' })
		const done = el('button', {
			class: 'primary',
			text: 'Done',
			title: 'Keep this session for the job and run it again',
			onclick: () => { done.disabled = true; send({ type: 'save-session' }) },
		})
		socket.addEventListener('message', (event) => {
			const data = JSON.parse(event.data)
			if (data.type === 'frame') {
				screen.src = `data:image/jpeg;base64,${data.data}`
				if (message.textContent === 'Starting the browser…') message.textContent = ''
			} else if (data.type === 'url') url.value = data.url
			else if (data.type === 'error') { message.textContent = data.message; done.disabled = false }
			else if (data.type === 'session-saved') { socket.close(); show(root) }
		})
		socket.addEventListener('close', () => { if (root.contains(screen)) message.textContent ||= 'The browser closed.' })
		const box = wireScreen(screen, send)
		root.replaceChildren(el('div', { class: 'harvester-recorder' },
			el('div', { class: 'harvester-header' },
				el('div', {},
					el('h2', { text: `${job.name}: continue in the browser` }),
					el('p', { class: 'harvester-muted', text: 'The site asks for a person. Answer it here as you would in your own browser (and sign in, if it asks); then press Done. The job keeps this session and runs again.' })),
				el('div', {}, done, el('button', { text: 'Cancel', onclick: () => { socket.close(); show(root) } }))),
			el('div', { class: 'harvester-toolbar' }, url),
			message,
			el('div', { class: 'harvester-stage' }, box)))
		screen.focusKeys()
	}

	// The recorder: the browser's screen, the person's mouse and keys to it, and the recipe so far.
	function openRecorder(root, page, textarea, apply) {
		let recipe = {}
		try {
			recipe = JSON.parse(textarea.value || '{}')
		} catch (e) {}
		let proposals = {}
		const suggestions = {}
		const socket = new WebSocket(RECORDER)
		const send = (message) => socket.readyState === WebSocket.OPEN && socket.send(JSON.stringify(message))
		const screen = el('img', { class: 'harvester-screen', tabindex: '0', alt: 'The recorder’s browser' })
		const url = el('input', { class: 'harvester-url', placeholder: 'https://… the site’s first page' })
		const message = el('p', { class: 'harvester-error' })
		const panel = el('div', { class: 'harvester-steps' })
		const modes = el('div', { class: 'harvester-modes' })
		const help = {
			record: 'Use the site as usual: sign in and go to the listing. Clicks, typing and pages opened here are recorded.',
			browse: 'Go anywhere without recording (into a year, an issue, …) to pick there.',
			follow: 'Click one link that leads to a further listing (a year, a category): similar links are picked with it.',
			links: 'Click one file link: the similar ones are picked with it, and a pattern is proposed.',
			next: 'Click the link to the next page of this listing (if there is one).',
		}
		const modeHelp = el('small', { text: help.record })
		const labels = [['record', 'Record steps'], ['browse', 'Browse'], ['follow', 'Pick links to follow'], ['links', 'Pick file links'], ['next', 'Pick next-page link']]
		for (const [mode, label] of labels) {
			const radio = el('input', { type: 'radio', name: 'harvester-mode', value: mode })
			radio.checked = mode === 'record'
			radio.addEventListener('change', () => { send({ type: 'mode', mode }); modeHelp.textContent = help[mode]; screen.focusKeys() })
			modes.append(el('label', {}, radio, ` ${label} `))
		}

		function renderPanel() {
			panel.replaceChildren(
				el('h3', { text: 'Steps' }),
				el('ol', {}, ...(recipe.steps || []).map((step) => el('li', { text: describe(step) }))),
				el('h3', { text: 'Listing' }),
				el('p', { text: recipe.start || '(the page where you pick)' }),
				...(recipe.follow || []).map((spec, level) =>
					renderLevel(`Level ${level + 1}: links to follow`, spec, proposals[`follow${level}`], send, 'follow', level, suggestions)),
				recipe.links && recipe.links.selector
					? renderLevel('File links', recipe.links, proposals.links, send, 'links', 0, suggestions)
					: el('p', { text: 'File links: not picked yet.' }),
				el('p', { text: `Next page: ${recipe.next ? `${recipe.next.selector} (level ${recipe.next.level})` : 'none'}` }))
		}

		function close(keep) {
			socket.close()
			if (keep) {
				textarea.value = JSON.stringify(recipe, null, 2)
				apply(suggestions)
			}
			root.replaceChildren(page)
		}

		socket.addEventListener('message', (event) => {
			const data = JSON.parse(event.data)
			if (data.type === 'frame') screen.src = `data:image/jpeg;base64,${data.data}`
			else if (data.type === 'url') url.value = data.url
			else if (data.type === 'error') message.textContent = data.message
			else if (data.type === 'recipe') {
				// What an earlier recording picked stays unless picked again.
				recipe = {
					...data.recipe,
					steps: data.recipe.steps.length ? data.recipe.steps : recipe.steps || [],
					start: data.recipe.start || recipe.start || '',
					follow: data.recipe.follow.length ? data.recipe.follow : recipe.follow || [],
					links: data.recipe.links && data.recipe.links.selector ? data.recipe.links : recipe.links || {},
					next: data.recipe.next || recipe.next,
				}
				proposals = data.proposals
				message.textContent = ''
				renderPanel()
			}
		})
		socket.addEventListener('close', () => { message.textContent ||= 'The recorder’s browser closed.' })

		const box = wireScreen(screen, send)

		root.replaceChildren(el('div', { class: 'harvester-recorder' },
			el('h2', { text: 'Recorder' }),
			el('p', { text: 'Sign in and go to the listing as you would (passwords are not recorded: the job uses its account). Then pick the links to follow (if the files are a level deeper) and the file links: their patterns are proposed, the matches outlined.' }),
			el('div', { class: 'harvester-toolbar' },
				url,
				el('button', { text: 'Go', onclick: () => send({ type: 'goto', url: url.value }) }),
				el('button', { text: 'Back', onclick: () => send({ type: 'back' }) }),
				el('button', { text: 'Undo step', onclick: () => send({ type: 'undo' }) }),
				el('button', { text: 'Clear picks', onclick: () => { recipe = { steps: recipe.steps }; send({ type: 'clear' }) } }),
				el('button', { class: 'primary', text: 'Done', onclick: () => close(true) }),
				el('button', { text: 'Cancel', onclick: () => close(false) })),
			modes, modeHelp, message,
			el('div', { class: 'harvester-stage' }, box, panel)))
		renderPanel()
		url.addEventListener('keydown', (event) => { if (event.key === 'Enter') send({ type: 'goto', url: url.value }) })
		url.focus()
	}

	// The input of an option, after its kind: an address, a pattern or a template (monospace), a
	// number with its bounds, text with suggestions.
	let suggestionLists = 0
	function optionInput(option, current) {
		const number = typeof option.default === 'number'
		const attributes = { value: current, type: option.kind === 'url' ? 'url' : number ? 'number' : 'text', spellcheck: 'false' }
		if (option.placeholder) attributes.placeholder = option.placeholder
		if (number && option.minimum !== null && option.minimum !== undefined) attributes.min = String(option.minimum)
		if (number && option.maximum !== null && option.maximum !== undefined) attributes.max = String(option.maximum)
		if (['regex', 'template'].includes(option.kind)) attributes.class = 'harvester-code'
		const input = el('input', attributes)
		if (option.suggestions && option.suggestions.length) {
			const id = `harvester-suggestions-${++suggestionLists}`
			input.setAttribute('list', id)
			// In the page already: a list the input refers to must be in the document.
			document.body.append(el('datalist', { id }, ...option.suggestions.map((value) => el('option', { value }))))
		}
		return input
	}

	// What an option shows below its input: a pattern's check as typed, a template's values to
	// insert at the caret, an address's check.
	function optionHelp(option, input) {
		if (option.kind === 'regex') {
			const verdict = el('small', { class: 'harvester-check-result' })
			const check = () => {
				if (!input.value) {
					verdict.textContent = ''
					return
				}
				try {
					// Python's named groups (?P<name>) as JavaScript writes them.
					new RegExp(input.value.replace(/\(\?P</g, '(?<'))
					verdict.textContent = '✓ a valid pattern'
					verdict.classList.remove('harvester-error')
				} catch (e) {
					verdict.textContent = `✗ ${e.message.replace(/^Invalid regular expression: /, '')}`
					verdict.classList.add('harvester-error')
				}
			}
			input.addEventListener('input', check)
			check()
			return verdict
		}
		if (option.kind === 'url') {
			const verdict = el('small', { class: 'harvester-check-result' })
			const check = () => {
				const bad = input.value && !/^https?:\/\/[^/\s]+/.test(input.value)
				verdict.textContent = bad ? '✗ an address starting with https://' : ''
				verdict.classList.toggle('harvester-error', Boolean(bad))
			}
			input.addEventListener('input', check)
			check()
			return verdict
		}
		if (option.kind === 'template' && option.values && option.values.length) {
			const insert = (name) => {
				const at = input.selectionStart ?? input.value.length
				const end = input.selectionEnd ?? at
				input.value = `${input.value.slice(0, at)}{${name}}${input.value.slice(end)}`
				input.focus()
				input.setSelectionRange(at + name.length + 2, at + name.length + 2)
			}
			return el('div', { class: 'harvester-values' }, el('small', { class: 'harvester-muted', text: 'Insert: ' }),
				...option.values.map((name) => el('button', { type: 'button', class: 'harvester-chip', text: `{${name}}`, onclick: () => insert(name) })))
		}
		return null
	}

	function renderForm(root, job, prefill) {
		const value = job || prefill || BLANK
		const name = el('input', { value: value.name, required: '' })
		const target = el('input', { value: value.target })
		const schedule = el('input', { value: value.schedule })
		// The schedule in words and its next runs in the person's time, as it is typed (the
		// server reads it as the executor does).
		const scheduleHint = el('p', { class: 'harvester-schedule', 'aria-live': 'polite' })
		let scheduleTimer
		const explainSchedule = () => {
			clearTimeout(scheduleTimer)
			scheduleTimer = setTimeout(async () => {
				try {
					const answer = await api('GET', `/schedule?expression=${encodeURIComponent(schedule.value.trim())}`)
					scheduleHint.classList.toggle('harvester-error', Boolean(answer.error))
					if (answer.error) {
						scheduleHint.textContent = answer.error
						return
					}
					const format = { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }
					const next = answer.next.map((at) => new Date(at * 1000).toLocaleString(undefined, format)).join(' · ')
					scheduleHint.replaceChildren(el('strong', { text: answer.description }), el('br'),
						el('span', { class: 'harvester-muted', text: `Next runs (your time): ${next}` }))
				} catch (e) {
					scheduleHint.textContent = ''
				}
			}, 250)
		}
		schedule.addEventListener('input', explainSchedule)
		explainSchedule()
		const tags = el('input', { value: value.tags.join(', ') })
		const enabled = el('input', { type: 'checkbox' })
		enabled.checked = value.enabled !== false
		const options = el('fieldset', {})
		const credentials = el('div', { class: 'harvester-credentials' })
		const error = el('p', { class: 'harvester-error' })
		const chosen = sources.find((s) => s.name === value.source) || sources[0]
		const optionInputs = {}
		let page

		// The recorder's proposals, when the person chose to use them.
		function applySuggestions(suggestions) {
			if (suggestions.path && optionInputs.path) optionInputs.path.value = suggestions.path
			if (suggestions.tags) tags.value = [...new Set([...tags.value.split(',').map((t) => t.trim()).filter(Boolean), ...suggestions.tags])].join(', ')
		}

		options.append(el('legend', { text: 'How the files are fetched' }))
		for (const [key, option] of Object.entries(chosen.options)) {
			const current = key in value.options ? value.options[key] : option.default
			if (option.kind === 'recipe') {
				const textarea = el('textarea', { rows: '10', class: 'harvester-recipe' })
				textarea.value = current
				optionInputs[key] = textarea
				options.append(field(option.title, textarea, option.description),
					el('button', { type: 'button', text: 'Record in the browser', onclick: () => openRecorder(root, page, textarea, applySuggestions) }))
			} else {
				if (option.kind === 'flag') {
					// On or off, kept as 1 or 0 (as the jobs saved before the checkbox).
					const box = el('input', { type: 'checkbox' })
					box.checked = Number(current) === 1
					optionInputs[key] = { get value() { return box.checked ? 1 : 0 } }
					options.append(el('label', { class: 'harvester-check' }, box,
						el('span', {}, el('strong', { text: option.title }), el('br'), el('small', { class: 'harvester-muted', text: option.description }))))
					continue
				}
				if (option.choices && option.choices.length) {
					// The values it may have; several (kept comma-separated) where it allows them.
					const chosen = String(current).split(',').map((v) => v.trim())
					const select = el('select', option.multiple ? { multiple: '', size: String(Math.min(option.choices.length, 7)) } : {},
						...option.choices.map((choice) => {
							const item = el('option', { value: choice, text: choice })
							item.selected = chosen.includes(choice)
							return item
						}))
					optionInputs[key] = {
						get value() { return [...select.selectedOptions].map((item) => item.value).join(',') || option.default },
					}
					options.append(field(option.title, select, option.description))
					continue
				}
				optionInputs[key] = optionInput(option, current)
				options.append(field(option.title, optionInputs[key], option.description))
				const extra = optionHelp(option, optionInputs[key])
				if (extra) optionInputs[key].after(extra)
				if (option.unit) {
					// A size in readable units beside the number, as it is typed.
					const readable = el('small', { class: 'harvester-size' })
					const update = () => { readable.textContent = sizeHint(optionInputs[key].value, option.unit) }
					optionInputs[key].addEventListener('input', update)
					optionInputs[key].after(readable)
					update()
				}
			}
		}

		const form = el('form', {
			class: 'harvester-form',
			onsubmit: async (event) => {
				event.preventDefault()
				const body = {
					name: name.value,
					source: chosen.name,
					target: target.value,
					schedule: schedule.value,
					tags: tags.value.split(','),
					enabled: enabled.checked,
					options: Object.fromEntries(Object.entries(optionInputs).map(([key, input]) =>
						[key, typeof chosen.options[key].default === 'number' ? Number(input.value) : input.value])),
				}
				try {
					const saved = await api(job ? 'PUT' : 'POST', job ? `/jobs/${job.id}` : '/jobs', body)
					// A new job's account can be set once it exists: the form stays open for it.
					if (job) show(root)
					else renderForm(root, saved)
				} catch (e) {
					error.textContent = e.message
				}
			},
		},
		el('h2', { class: 'harvester-with-icon' }, icon(chosen.name), el('span', { text: job ? `Edit ${job.name}` : `New job: ${chosen.title}` })),
		field('Name', name),
		field('Folder', target, 'In your files, e.g. /Magazines/iX.'),
		field('Schedule', schedule, 'Cron: minute hour day month weekday (UTC). 0 6 * * * is daily at 06:00.'),
		scheduleHint,
		field('Tags', tags, 'Comma-separated; may use the link’s values, e.g. {year}. A tag whose value a file lacks is left out.'),
		el('label', { class: 'harvester-check' }, enabled, el('span', { text: 'Enabled: runs on its schedule' })),
		options,
		error,
		el('button', { class: 'primary', type: 'submit', text: 'Save' }),
		el('button', { type: 'button', text: 'Back to the jobs', onclick: () => show(root) }))
		page = el('div', {}, form, credentials)
		root.replaceChildren(page)
		renderCredentials(credentials, job, chosen)
	}

	async function show(root) {
		try {
			await renderJobs(root)
		} catch (e) {
			root.replaceChildren(el('p', { class: 'harvester-error', text: `Harvester: ${e.message}` }))
		}
	}

	// Nextcloud's look: its colours, radii and spacing variables, its buttons and inputs.
	document.head.append(el('style', {}, `
		#harvester {
			flex: 1 1 auto; min-width: 0; overflow: auto;
			background: var(--color-main-background); color: var(--color-main-text);
			border-radius: var(--body-container-radius, var(--border-radius-large));
			padding: calc(var(--default-grid-baseline, 4px) * 6) calc(var(--default-grid-baseline, 4px) * 8);
		}
		#harvester h2 { margin: 0 0 4px; font-size: 1.5em; }
		#harvester h3 { margin: 16px 0 8px; font-size: 1.15em; }
		#harvester .harvester-muted { color: var(--color-text-maxcontrast); }
		#harvester .harvester-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 16px; }
		#harvester .harvester-header p { margin: 0; }
		#harvester .harvester-empty { padding: 48px 0; text-align: center; }
		#harvester table { width: 100%; border-collapse: collapse; }
		#harvester th { text-align: start; font-weight: normal; color: var(--color-text-maxcontrast); padding: 8px; border-bottom: 1px solid var(--color-border); }
		#harvester td { padding: 10px 8px; border-bottom: 1px solid var(--color-border); vertical-align: top; white-space: normal; }
		#harvester tbody tr:hover { background: var(--color-background-hover); }
		#harvester .harvester-actions { text-align: end; }
		#harvester .harvester-actions button { margin-block-end: 4px; }
		#harvester td code { white-space: nowrap; }
		#harvester .harvester-message { max-width: 420px; overflow-wrap: anywhere; white-space: normal; }
		#harvester .harvester-actions button { margin-inline-start: 4px; }
		#harvester .harvester-chip { display: inline-block; margin: 0 4px 2px 0; padding: 0 8px; border-radius: var(--border-radius-pill, 999px); background: var(--color-background-dark); font-size: 0.9em; }
		#harvester .harvester-status { display: inline-block; padding: 0 8px; border-radius: var(--border-radius-pill, 999px); font-size: 0.9em; background: var(--color-background-dark); }
		#harvester .harvester-status-ok { background: var(--color-success); color: var(--color-success-text, #fff); }
		#harvester .harvester-status-failed, #harvester .harvester-status-auth { background: var(--color-error); color: var(--color-error-text, #fff); }
		#harvester .harvester-status-running { background: var(--color-primary-element); color: var(--color-primary-element-text); }
		#harvester .harvester-status-challenge { background: var(--color-warning); color: var(--color-warning-text, #000); }
		#harvester .harvester-field { display: flex; flex-direction: column; gap: 2px; margin: 12px 0; max-width: 700px; }
		#harvester .harvester-field > span { font-weight: bold; }
		#harvester .harvester-check { display: flex; align-items: center; gap: 8px; margin: 12px 0; }
		#harvester .harvester-field small { color: var(--color-text-maxcontrast); }
		#harvester .harvester-size { font-variant-numeric: tabular-nums; }
		#harvester .harvester-with-icon { display: flex; align-items: center; gap: 8px; }
		#harvester .harvester-icon { flex: none; color: var(--color-primary-element); }
		#harvester td.harvester-with-icon { display: table-cell; white-space: nowrap; }
		#harvester td.harvester-with-icon .harvester-icon { vertical-align: middle; margin-inline-end: 6px; }
		#harvester .harvester-code { font-family: var(--font-face-monospace, monospace); }
		#harvester .harvester-check-result { color: var(--color-success-text, var(--color-success)); }
		#harvester .harvester-check-result.harvester-error { color: var(--color-error-text, var(--color-error)); }
		#harvester .harvester-values { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; margin-top: 4px; }
		#harvester .harvester-values .harvester-chip { border: none; min-height: 0; padding: 2px 8px; cursor: pointer; font-family: var(--font-face-monospace, monospace); }
		#harvester .harvester-values .harvester-chip:hover { background: var(--color-primary-element-light); }
		#harvester .harvester-schedule { margin: -4px 0 12px; max-width: 700px; }
		#harvester select[multiple] { height: auto; min-height: 0; padding: 4px; }
		#harvester select[multiple] option { padding: 2px 8px; border-radius: var(--border-radius); }
		#harvester .harvester-field input:not([type=checkbox]), #harvester .harvester-field select, #harvester .harvester-field textarea { width: 100%; margin: 0; }
		#harvester fieldset { border: 1px solid var(--color-border); border-radius: var(--border-radius-large); padding: 8px 16px; margin: 16px 0; max-width: 760px; }
		#harvester legend { padding: 0 4px; font-weight: bold; }
		#harvester form > button, #harvester .harvester-card button { margin: 8px 8px 0 0; }
		#harvester .harvester-error { color: var(--color-error-text, var(--color-error)); }
		#harvester .harvester-credentials { margin-top: 24px; max-width: 760px; }
		#harvester .harvester-kinds { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; margin-bottom: 8px; }
		#harvester .harvester-kinds .harvester-card { margin: 0; max-width: none; display: flex; flex-direction: column; }
		#harvester .harvester-kinds .harvester-card p { flex: 1; }
		#harvester .harvester-card { border: 1px solid var(--color-border); border-radius: var(--border-radius-large); padding: 12px 16px; margin: 12px 0; max-width: 760px; }
		#harvester .harvester-recipe, #harvester .harvester-pattern { font-family: var(--font-face-monospace, monospace); }
		#harvester .harvester-toolbar { display: flex; gap: 4px; margin: 8px 0; align-items: center; }
		#harvester .harvester-url { flex: 1; margin: 0; }
		#harvester .harvester-modes label { margin-inline-end: 12px; }
		#harvester .harvester-stage { display: flex; gap: 12px; align-items: flex-start; margin-top: 8px; }
		#harvester .harvester-screen-box { flex: 3; min-width: 0; max-width: calc((100vh - 260px) * 1.6); position: relative; }
		#harvester .harvester-screen {
			display: block; width: 100%; aspect-ratio: 1280 / 800;
			border: 1px solid var(--color-border); border-radius: var(--border-radius); background: var(--color-background-dark); cursor: crosshair;
		}
		#harvester .harvester-keys { position: absolute; inset: 0; width: 100%; height: 100%; opacity: 0; resize: none; pointer-events: none; border: 0; padding: 0; }
		#harvester .harvester-screen-box:focus-within .harvester-screen { outline: 2px solid var(--color-primary-element); }
		#harvester .harvester-steps { flex: 1; min-width: 300px; max-height: 80vh; overflow: auto; overflow-wrap: anywhere; font-size: 90%; }
		#harvester .harvester-level { border-inline-start: 3px solid var(--color-primary-element); padding-inline-start: 8px; margin: 8px 0; }
		#harvester .harvester-value { background: var(--color-primary-element-light); border-radius: 4px; padding: 0 4px; margin-inline-end: 4px; font-family: var(--font-face-monospace, monospace); }
	`))

	document.addEventListener('DOMContentLoaded', async () => {
		const root = el('div', { id: 'harvester' })
		// AppAPI's page is an empty <div id="content"> inside Nextcloud's <main id="content">: with
		// Nextcloud's styles for #content it is a fixed box over the page, and anything next to it
		// is squeezed and covered. Harvester takes its place as the page's content area.
		const placeholder = document.querySelector('#content #content')
		if (placeholder) placeholder.replaceWith(root)
		else (document.querySelector('#content') || document.body).append(root)
		;[sources, presets] = await Promise.all([api('GET', '/sources'), api('GET', '/presets')])
		show(root)
	})
})()
