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
				el('td', {}, el('strong', { text: job.name }), job.enabled ? '' : el('span', { class: 'harvester-chip', text: 'paused' })),
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
				el('h3', { text: preset.title }), el('p', { text: preset.description }), ...fields,
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
			el('h3', { text: source.title }),
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

	function renderForm(root, job, prefill) {
		const value = job || prefill || BLANK
		const name = el('input', { value: value.name, required: '' })
		const target = el('input', { value: value.target })
		const schedule = el('input', { value: value.schedule })
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
				optionInputs[key] = el('input', { value: current, type: typeof option.default === 'number' ? 'number' : 'text' })
				options.append(field(option.title, optionInputs[key], option.description))
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
		el('h2', { text: job ? `Edit ${job.name}` : `New job: ${chosen.title}` }),
		field('Name', name),
		field('Folder', target, 'In your files, e.g. /Magazines/iX.'),
		field('Schedule', schedule, 'Cron: minute hour day month weekday (UTC). 0 6 * * * is daily at 06:00.'),
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
