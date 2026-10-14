# superhero

tis be a bot, tis be superhero.

Two Reddit listeners share one engine in `app/main.py`:

- **superhero** watches eyewear subreddits for posts a model judges worth
  reading, and emails them to you hourly.
- **business** watches small-business subreddits for people signalling they need
  to hire help, and emails those leads.

Each runs as its own service with `--bot superhero` or `--bot business`, and
reads its own credentials, keywords and state files.

## Guide topics (superhero only)

The posts themselves are not publishable, but the questions inside them are. The
superhero bot reads the questions out of the posts that survived the relevance
filter, drops the duplicates, and emails you the backlog once a day so you can
write a guide for each one and post it on the site.

```powershell
python -m app.main --bot superhero
```

The daily list goes out after `QUESTIONS_HOUR` UTC (23:00 by default). It is a
work queue, not a daily trickle: anything you have not been sent yet comes back
tomorrow, so you work through it at your own pace instead of falling behind.

It goes to `QUESTIONS_RECIPIENT_EMAIL` and to nobody else. The post digest cc's
`CC_EMAILS`, but the guide list deliberately never does, since it is a work
queue for whoever writes the articles rather than something to pass around.

Questions come out phrased the way someone would type them into a search box,
with the subject filled in from the post when the post left it implicit. They
exclude anything that needs the poster's own face, prescription or budget to
answer, since no article could answer those.

`superhero_questions.json` is the backlog, and it is meant to be edited by hand.
Clearing a question's `sent_on` puts it back on tomorrow's list, which is how a
topic comes back around once you have had another look at it.

The file lives on the Railway volume when there is one. It has to: a redeploy
would otherwise wipe it and re-send every question the following evening.

Try it without waiting for the evening with a single sweep:

```powershell
python -m app.main --bot superhero --once
```

That prints the questions it found instead of waiting for the hour.

Useful flags: `--no-questions` skips this pass entirely, `--questions-hour` moves
the send, and `--questions-path` points the backlog somewhere else.

## Setup

```powershell
pip install -r requirements.txt
copy .env.example .env
```

`GROQ_API_KEY` does the relevance and question passes. Without it the bot
collects nothing, rather than sending you everything unfiltered. `BREVO_API_KEY`,
`SENDER_EMAIL` and `RECIPIENT_EMAIL` do the sending; without them the bot prints
what it found instead of emailing it.

## Tests

```powershell
python -m unittest discover -s tests
```
