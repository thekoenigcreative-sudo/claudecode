"""Plain language for the Trader chat: what Rick means, worked out in code.

Rick, 25 Sep 2026: "i need to be able to just tell it things without commands". Every command
the chat has (/status, /stop, /model, /think, /queue, /new, /reset, /change, /undo, /changes,
/help, /positions) also works from an ordinary sentence - "how's it going today", "what did
it trade", "stop it for today", "use opus for the decider", "why did it pass on NWL", "show
me the positions" - and a question about the day's trading is answered from the arena's
records (arena/today.py), never guessed. The commands stay as shortcuts.

`understand(text, known_codes)` turns a message into an Intent or None. It is plain pattern
matching: no model reads Rick's words here, so what a sentence does is decided in code and
testable line by line (tests/test_plain.py). None means the message is not one of the
things the chat does itself: it goes on to the change-request check and then to the decider
as a conversation, as before.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass, field

# The ordered ladder "think harder" and "think less" step along (botctl.THINK_LEVELS also
# has off and adaptive, which are not steps on it).
THINK_LADDER = ["minimal", "low", "medium", "high", "xhigh", "max"]


@dataclass
class Intent:
    name: str
    args: dict = field(default_factory=dict)

    def __getattr__(self, item):  # intent.agent, intent.tickers ...
        try:
            return self.args[item]
        except KeyError:
            raise AttributeError(item) from None


# --------------------------------------------------------------------------- normalising

_PREFIX = re.compile(
    r"^(?:(?:hey|hi|hello|yo|ok|okay|so|and|now|right|also|thanks|cheers|please|pls|mate|"
    r"trader|bot)[,!.\s]+)*"
    r"(?:(?:can|could|would|will|pls|please)\s+you\s+(?:please\s+)?(?:just\s+)?)?"
    r"(?:(?:i(?:'d| would)? (?:like|want) (?:you )?to|i need (?:you )?to|let'?s|"
    r"go ahead and|just|quickly|please)\s+)?",
    re.I,
)
_SUFFIX = re.compile(r"(?:[\s,]+(?:please|pls|thanks|thank you|cheers|mate|ta))*[\s?!.]*$",
                     re.I)  # fmt: skip


def normalise(text: str) -> str:
    """Lower case, straight quotes, one space, and the politeness trimmed off both ends:
    'Hey, could you please show me the positions?' -> 'show me the positions'."""
    t = (text or "").replace("’", "'").replace("‘", "'").strip()
    t = re.sub(r"\s+", " ", t).lower()
    t = _SUFFIX.sub("", t)
    t = _PREFIX.sub("", t)
    return t.strip()


# --------------------------------------------------------------------------- tickers

# Words that are also ASX codes (or look like them) and never mean a stock in a sentence.
_NOT_CODES = {
    "ASX", "IBKR", "IB", "AI", "OK", "OKAY", "PNL", "AUD", "USD", "ETF", "CEO", "CFO", "EOD",
    "VWAP", "RVOL", "AGM", "PDF", "GMT", "AEST", "AEDT", "DST", "API", "LLM", "AM", "PM", "ID",
    "FYI", "ASAP", "BTW", "IMO", "TBH", "LOL", "PS", "NB", "EG", "IE", "VS", "ATM", "IPO",
    "P&L", "GO", "NO", "YES", "HI", "HEY", "YO", "TA", "SO", "IT", "IS", "ME", "MY", "OR",
    "ON", "IN", "AT", "TO", "UP", "DO", "BE", "BY", "OF", "AN", "AS", "IF", "WE", "US",
}
# Ordinary English words that happen to be listed codes (ALL, AND, ANY ...): a lower-case
# word is only a stock when it is not one of these; in capitals (ALL) it is.
_COMMON = set("""
about above add after again ago all also and any are ask back bad bar bars been best big
bit bot both but buy can cap car cash cut day did dip dog dot down due each end even ever
far fee few fix flat for from fun gap get give got had has have hey him his hit hold hot how
its job just keep key kid last late led let lie log lot low man map max may men mid mix mod
net new nil nod not now odd off oil old one only opt our out own pay per pet pin pit pop pot
pre pro pub put ran raw red rid rip rod row rub run sad saw say see set she sir sit six sky
son sum sun tag tap tax tea ten the tie tin tip toe ton too top toy try two use van via war
was way web wed wet who why win wit won yes yet you zip
""".split())


def tickers_in(text: str, known: Collection[str]) -> list[str]:
    """The ASX codes named in a sentence, in order. A code in capitals (NWL) counts when the
    directory knows it; a lower-case one (nwl) only when it is not an ordinary word."""
    out: list[str] = []
    known_up = {str(k).upper() for k in known}
    for tok in re.findall(r"\b[A-Za-z][A-Za-z0-9]{1,4}\b", text or ""):
        up = tok.upper()
        if up in _NOT_CODES or up not in known_up or up in out:
            continue
        if tok == up or tok.lower() not in _COMMON:
            out.append(up)
    return out


# --------------------------------------------------------------------------- patterns

_AGENT = re.compile(r"\b(reader|decider|both(?: of them| agents)?|all of them|them both|"
                    r"everyone|everything|all)\b")  # fmt: skip
_MODEL = re.compile(r"\b(opus|sonnet|haiku)(?:[\s-]*(\d+(?:[.\-]\d+)?))?\b")
_DEFAULT = re.compile(r"\b(?:back to (?:normal|default|usual|standard|how it was|its usual|"
                      r"the default)|(?:normal|default|usual|standard) (?:model|models|level|"
                      r"thinking|effort)|reset|default|un-?pin)\b")  # fmt: skip
_LEVEL = re.compile(r"\b(off|minimal|low|medium|high|x-?high|extra[ -]high|max|maximum|"
                    r"adaptive)\b")  # fmt: skip
_UP = re.compile(r"\b(harder|more|deeper|longer|up|higher|increase|raise|smarter|boost|"
                 r"bump up|crank up|turn up)\b")  # fmt: skip
_DOWN = re.compile(r"\b(less|easier|lighter|quicker|faster|shorter|down|lower|decrease|"
                   r"reduce|dumber|cheaper|turn down|dial down|ease off)\b")  # fmt: skip
_THINK = re.compile(r"\b(think|thinks|thinking|thought|effort|reason|reasoning|brain|"
                    r"brainpower)\b")  # fmt: skip
_QUESTION = re.compile(r"^(?:what|which|who|why|how|is|are|does|do|has|have|did|was|were|"
                       r"should|would|could|will|can it|can you tell|tell me)\b")  # fmt: skip
# A setting is changed only by a sentence shaped as an instruction ("use opus for the
# decider", "make the reader think harder") or a short one ("decider back to normal",
# "think less"): a remark that merely mentions a model or a level ("the decider used opus
# yesterday and passed") is conversation, never a strategy change.
_SET_VERB = re.compile(r"^(?:set|put|make|turn|dial|crank|switch|change|let|have|get|bump|"
                       r"raise|lower|increase|decrease|reduce|drop|move|give|use|run|go|try|"
                       r"i (?:want|need|would like|'d like)|let'?s|from now on)\b")  # fmt: skip
SHORT = 6  # words


def _instruction(t: str) -> bool:
    return bool(_SET_VERB.match(t)) or len(t.split()) <= SHORT


_ORDER_REQUEST = re.compile(
    r"^(?:(?:close|sell|buy|short|cover|exit|dump|get out of|get into|place|enter|go long|"
    r"go short|take profit|add to|double|flatten|liquidate|unwind|bail on|put on)\b"
    r"(?! (?:the |a )?(?:question|note|look))|open (?:a |an |the |another |new ))"
)
_STOP = re.compile(
    r"^(?:stop|cancel|abort|halt|kill|never ?mind|nevermind|forget (?:it|that)|leave it|"
    r"hold on|hang on|drop (?:it|that))"
    r"(?:\s+(?:it|that|this|now|please|everything|trading|the answer|what you'?re doing|"
    r"working|working on (?:it|that|this)|for (?:today|now|the day)|for the rest of (?:the "
    r"day|today)|today|the (?:bot|trader|arena|watcher|trading)|answering|talking|"
    r"thinking|there))*$"
)
_STOP_TRADING = re.compile(
    r"^(?:(?:stop|pause|halt|suspend|switch off|turn off|shut down|shut off|kill|disable|"
    r"park|freeze)\s+(?:it|the (?:bot|trader|arena|watcher|trading)|trading|everything|all "
    r"trading|all trades|it trading|it all)(?:\s+(?:for (?:today|now|the day)|today|now|for "
    r"the rest of (?:the day|today)|until tomorrow|for a bit|for a while))?"
    r"|(?:that'?s|thats) enough (?:trading )?for today|no more (?:trades|trading)(?: today)?|"
    r"don'?t (?:trade|do) (?:any ?more|anything else)(?: today)?|take (?:the rest of )?"
    r"(?:the day|today) off|call it a day|pack it in(?: for today)?)$"
)
_STOP_TRADING_WORDS = re.compile(r"\b(trading|today|the day|bot|trader|arena|watcher|pause|"
                                 r"shut|switch off|turn off|disable|suspend|park|freeze|"
                                 r"tomorrow)\b")  # fmt: skip
_HELP = re.compile(
    r"^(?:help|help me|what can (?:you|i) (?:do|say|ask(?: you)?|tell you)|what (?:do|can) you "
    r"do|what are you able to do|how does this (?:chat |thing )?work|how do i (?:use|talk to|"
    r"work) (?:this|you|it)|what are (?:the |your |my )?(?:commands|options|choices)|what do "
    r"i (?:say|type|ask)|(?:show me |list |the )?(?:commands|options)|instructions|menu|what "
    r"is this|who are you|what are you)$"
)
_NEW = re.compile(
    r"^(?:start (?:over|again|afresh|fresh|a new (?:chat|conversation|session|thread|topic)|"
    r"a fresh (?:chat|conversation|session))|new (?:chat|conversation|session|thread|topic)|"
    r"fresh (?:start|chat|conversation|session)|reset(?: the| this| our)? (?:chat|"
    r"conversation|session|context|thread|history)|clear (?:the |this |our |your )?(?:chat|"
    r"conversation|context|history|slate|session|memory)|forget (?:everything|all (?:of )?"
    r"that|all that|what we (?:said|talked about|discussed|were saying)|(?:the|this|our) "
    r"(?:chat|conversation))|wipe the slate(?: clean)?|clean slate|reset)$"
)
_UNDO = re.compile(
    r"^(?:undo|roll ?back|revert|reverse|back out|put (?:it |that |things )?back)"
    r"(?:\s+(?:that|it|this|the|your|my|last|latest|previous|recent|change|changes|one|"
    r"thing|update|what you (?:just )?(?:did|changed|built)|how (?:it|they|things) (?:was|"
    r"were)|to how (?:it|they|things) (?:was|were)|please))*$"
)
_CHANGES = re.compile(
    r"\b(?:change requests?|changes? (?:i(?:'ve| have)? (?:asked|requested|sent)|pending|so "
    r"far|list|log|history|queue|status|in progress|underway|being built|outstanding)|what "
    r"have (?:i|you) (?:asked|changed|requested|built)|what (?:did|have) i ask(?:ed)?(?: you)? "
    r"(?:for|to change)|where(?:'s| is| are) (?:my|the|that) change|recent changes|(?:any|"
    r"list|show|what|which|my|the) (?:the |my |recent |pending |outstanding )?changes|pending "
    r"changes|is (?:my|the|that) change (?:done|live|built|ready|in yet))\b"
)
_QUEUE_SHOW = re.compile(
    r"\b(?:queue (?:mode|setting|settings|status|is)|what happens (?:if|when) i (?:message|"
    r"send|text|write|say)|while (?:you'?re|it'?s|you are|it is) busy|how do you (?:handle|"
    r"deal with) (?:messages|it|things) (?:when|while)|what'?s the queue|how'?s the queue)\b"
)
_QUEUE_SET = [
    (re.compile(r"\b(?:answer (?:them |my messages |messages |each one |each |everything )?"
                r"(?:one at a time|in turn|in order|one by one|each in turn)|(?:one at a time|"
                r"in turn|one by one)$|follow ?up mode)\b"), "followup"),  # fmt: skip
    (re.compile(r"\b(?:bundle|collect|batch|group|gather|combine) (?:my |the |up )?"
                r"(?:messages|them)|collect mode\b"), "collect"),  # fmt: skip
    (re.compile(r"\b(?:interrupt (?:mode|me|the answer|yourself)|interrupt$)\b"),
     "interrupt"),  # fmt: skip
    (re.compile(r"\bsteer (?:mode|me)\b"), "steer"),
    (re.compile(r"\bqueue\b.*\b(?:default|normal|back to)\b|\b(?:default|normal) queue\b"),
     "default"),  # fmt: skip
    (re.compile(r"\bqueue (?:mode )?(?:to )?(steer|followup|follow-up|collect|interrupt)\b"),
     None),  # fmt: skip
]
_MODEL_SHOW = re.compile(
    r"\b(?:(?:what|which) (?:ai |llm |claude )?models?\b|models? (?:is|are) (?:it|you|they|the "
    r"reader|the decider|the agents?) (?:on|using|running)|what(?:'s| is| are) (?:it|you|they|"
    r"the reader|the decider|the agents?) (?:running on|on|using)|what (?:is|are) (?:it|you|"
    r"they) running on|(?:current|which) models?|model settings?|(?:show|list) (?:me )?(?:the )?"
    r"models?|models? (?:allowed|available|list)|^models?$)"
)
_THINK_SHOW = re.compile(
    r"\b(?:how hard (?:is|are|does|do) (?:it|you|they|the reader|the decider|the agents?) "
    r"think(?:ing)?|(?:what|which) (?:thinking|effort|reasoning)(?: level| setting)?|(?:thinking"
    r"|effort) levels?$|how much (?:thinking|effort)|what level (?:is|are) (?:it|they|you|the "
    r"reader|the decider) (?:on|at|thinking))"
)
_STORY_Q = re.compile(
    r"\b(?:why|what happened|what did|what(?:'s| is) (?:going on|the story|happening|up|the "
    r"deal|the news)|tell me about|how did|how(?:'s| is|'d| did)|did (?:it|you|we|the bot|the "
    r"agent|the decider)|any(?:thing)? (?:on|about|with|for|happen)|what about|explain|reason|"
    r"pass(?:ed)?|skip(?:ped)?|reject(?:ed)?|trade[ds]?|took|take|do with|done with|record|"
    r"story|history|status (?:of|on)|update on|news on|happen(?:ed|ing)? (?:with|to|on)|look "
    r"at|looked at|see|saw|miss(?:ed)?|ignore[d]?|decision on|thoughts on|what(?:'s| is) "
    r"(?:it|the bot|the agent|the decider) (?:doing|done) (?:with|on|about))\b"
)
_TRADES = re.compile(
    r"\b(?:what (?:did|has|have|does|is) (?:it|you|we|the bot|the agent|the decider|the trader|"
    r"the arena|they) (?:trade|traded|buy|bought|sell|sold|short|shorted|fill|filled|"
    r"trading|buying|selling|been trading)|(?:any|all|the|what|which|show me the|show the|"
    r"show me|list the|list|today'?s|our|its|your|recent) ?(?:trades?|fills?|orders?|buys?|"
    r"sells?|executions?|deals?)(?: (?:today|so far|for today|placed|filled|done|went "
    r"through|this morning|this afternoon|yet))?\b|(?:trades?|fills?|orders?) (?:today|so "
    r"far|for today|placed|filled|yet)|did (?:it|you|we|they) (?:trade|buy|sell|do anything|"
    r"place|fill|take)(?: anything| any| something)?|what (?:got|was|has been|'s been|were) "
    r"(?:filled|traded|bought|sold|placed|executed)|(?:trade|fill|order) (?:list|log|"
    r"history|book)|how many (?:trades|fills|orders)|(?:has|have) (?:it|we|you) traded|"
    r"trading (?:today|so far|log|activity)|traded (?:today|anything|yet))\b"
)
_PNL = re.compile(
    r"\b(?:p ?& ?l|pnl|p and l|profit|profits|profitable|loss|losses|how much (?:have we|has "
    r"it|did it|did we|are we|is it|have you|did you) (?:made|lost|up|down|won|earned|won or "
    r"lost|made or lost)|(?:up|down) (?:or down |or up )?(?:today|so far|on the day|for the "
    r"day)|balance|balances|equity|how(?:'s| is| are) the (?:money|account|accounts|bank|"
    r"bankroll|books?)|(?:are we|is it|am i) (?:up|down|ahead|behind|winning|losing|in the "
    r"(?:red|black|green)|in profit|making money|losing money)|net (?:result|position|"
    r"figure)|score(?:board)?|the money|made money|lost money|(?:winning|losing) or|bottom "
    r"line|how much (?:money|cash)|what(?:'s| is) (?:it|the account|the arena) worth|"
    r"returns?)\b"
)
_POSITIONS = re.compile(
    r"\b(?:positions?|holdings?|what(?:'s| is) open|what (?:are|do|does|is|am) (?:we|you|it|"
    r"the bot|the agent|i) (?:hold|holding|have|own|got|have on|in|long|short|carrying)|"
    r"what(?:'s| is) in the (?:book|books|portfolio|accounts?)|portfolio|exposure|(?:anything|"
    r"what|something) (?:still )?open|open (?:trades|book)|show me the book|the book|what "
    r"(?:are we|is it|am i) (?:in|long|short)|what have we got on|still in (?:anything|"
    r"something))\b"
)
_RULES_Q = re.compile(
    r"\b(?:can it|can you|allowed|limit|limits|maximum|max|minimum|min|how many .* (?:can|"
    r"allowed|may|able)|rule|rules|cap|caps|policy)\b"
)
_TASKS = re.compile(
    r"\b(?:what(?:'s| is| are) (?:you |it )?(?:doing|running|working on)(?: (?:right )?now)?|"
    r"anything running|what(?:'s| is) running|what are you up to|busy\?*$|are you busy|"
    r"running now)\b"
)
_STATUS = re.compile(
    r"^(?:status|state|sitrep|health|check ?in|all good|everything (?:ok|okay|alright|fine|"
    r"good)|you (?:ok|okay|alive|there|awake|up|running|still there)|still there|still "
    r"running|still up|alive|running)$"
    r"|\b(?:is (?:it|the watcher|the bot|the trader|the arena|everything|the system|"
    r"anything) (?:running|up|alive|on|working|ok|okay|alright|fine|healthy|down|dead|"
    r"off|stopped|broken|still (?:running|up|going|alive|working))|are you (?:running|up|"
    r"there|alive|awake|ok|okay|working|still there|on)|(?:watcher|bot|system|arena) status|"
    r"what(?:'s| is) (?:the |your |its )?(?:status|state)|is (?:everything|it all|all) (?:ok|"
    r"okay|fine|good|alright|working|well|running)|what(?:'s| is) the watcher doing|has (?:it|"
    r"the watcher) (?:started|stopped|crashed|died)|did (?:it|the watcher) (?:start|stop|"
    r"crash|die))\b"
)
_TODAY = re.compile(
    r"\b(?:how(?:'s| is| has| was|'d| did|s)? (?:it|today|the day|things|trading|everything|"
    r"the arena|the trader|the bot|we|you|it all) (?:going|been|gone|go|doing|done|getting "
    r"on|looking|tracking|travelling|traveling|faring|been going|going today|gone today|"
    r"been today)|how(?:'s| is) (?:it|today|everything|things|the day)\b|how (?:are|is) "
    r"(?:things|we|it|the day|the arena)(?: (?:going|doing|looking|tracking|getting on))?|"
    r"how(?:'d| did) (?:it|we|today|the day|you|things) (?:go|do)|what(?:'s| has| is) "
    r"(?:happened|happening|been happening|going on|new|the news|the latest|the story|the "
    r"state of play|the score|the damage|up)|what (?:happened|is happening|has happened|"
    r"went on)|what (?:did|has|have) (?:it|you|we|the bot|the trader|the arena|they) (?:do|"
    r"done|been (?:up to|doing)|get up to)|today'?s (?:summary|results?|rundown|recap|report|"
    r"numbers|story)|(?:give me |send me |show me )?(?:a |an |the )?(?:rundown|summary|"
    r"update|recap|report|overview|debrief|wrap|wrap-up|wrapup)(?: on| of| for)?(?: today| "
    r"the day)?|update me|fill me in|catch me up|bring me up to (?:speed|date)|where are we "
    r"(?:at|up to)|anything (?:to report|interesting|happen(?:ed)?|going on|new)|how did "
    r"the day go|good day or bad|was it a good day|how did we go|how was (?:it|today|the "
    r"day|your day)|how(?:'s| is) (?:my|the) (?:bot|trader) (?:doing|going))\b"
)
_HOURS = re.compile(
    r"\b(?:when (?:does|do|did|will|is|are|should) (?:it|the watcher|trading|the arena|the "
    r"bot|the market|the report|the evening report|you|the day) (?:start|stop|run|open|"
    r"close|finish|end|go|come|due|arrive|sent|kick off|knock off|begin|wake up|shut down)|"
    r"what time (?:does|do|will|is|did)|(?:trading|watcher|market|arena|its|the|your|"
    r"opening|operating|working) hours|what (?:are|is) (?:the|its|your) (?:hours|schedule|"
    r"timetable|window|times)|when (?:is|does|will) the (?:report|evening report)|daylight "
    r"saving|dst|what time (?:is|does) (?:the|it))\b"
)
_WHOAMI = re.compile(r"\b(?:my (?:telegram )?(?:id|user id|chat id)|who am i|whoami)\b")


# --------------------------------------------------------------------------- understanding


def _agent(t: str) -> str | None:
    m = _AGENT.search(t)
    if not m:
        return None
    w = m.group(1)
    if w.startswith("reader"):
        return "reader"
    if w.startswith("decider"):
        return "decider"
    return "both"


def _model_value(t: str) -> str | None:
    m = _MODEL.search(t)
    if m:
        fam, num = m.group(1), m.group(2)
        return fam + ("-" + re.sub(r"[.\-]", "-", num) if num else "")
    if _DEFAULT.search(t):
        return "default"
    return None


def _level_value(t: str) -> str | None:
    m = _LEVEL.search(t)
    if m:
        w = m.group(1).replace(" ", "-")
        return {"x-high": "xhigh", "extra-high": "xhigh", "maximum": "max"}.get(w, w)
    if _DEFAULT.search(t):
        return "default"
    if _UP.search(t):
        return "up"
    if _DOWN.search(t):
        return "down"
    return None


def understand(text: str, known_codes: Collection[str] = ()) -> Intent | None:
    """What Rick's message asks the chat to do itself, or None (a change request, or a
    conversation with the decider). Checked in this order, most specific first."""
    raw = (text or "").strip()
    if not raw or raw.startswith("/"):
        return None
    t = normalise(raw)
    if not t:
        return None
    tickers = tickers_in(raw, known_codes)

    if _ORDER_REQUEST.match(t) and not _STOP.match(t):
        return Intent("order_request", {"tickers": tickers})
    if _STOP.match(t) or _STOP_TRADING.match(t):
        return Intent("stop", {"trading": bool(_STOP_TRADING.match(t)
                                                or _STOP_TRADING_WORDS.search(t))})
    if _HELP.match(t):
        return Intent("help")
    if _NEW.match(t):
        return Intent("new", {"reset": t.startswith(("reset", "clear", "wipe", "clean"))})
    if _UNDO.match(t):
        return Intent("undo")
    if _CHANGES.search(t):
        return Intent("changes")
    for pat, mode in _QUEUE_SET:
        m = pat.search(t)
        if m:
            return Intent("queue_set", {"mode": (mode or m.group(1)).replace("follow-up",
                                                                             "followup")})
    if _QUEUE_SHOW.search(t):
        return Intent("queue_show")

    # The agents' models and effort levels: a question shows them, anything else sets one.
    has_model = _model_value(t) is not None and _MODEL.search(t) is not None
    if _THINK_SHOW.search(t):
        return Intent("think_show")
    if _MODEL_SHOW.search(t) or (has_model and _QUESTION.match(t)):
        return Intent("model_show")
    instruction = _instruction(t) and not _QUESTION.match(t)
    if _THINK.search(t) and instruction:
        level = _level_value(t)
        if level:
            return Intent("think_set", {"agent": _agent(t), "level": level})
    if has_model and instruction:
        return Intent("model_set", {"agent": _agent(t), "model": _model_value(t)})
    if (_DEFAULT.search(t) and _agent(t) and instruction
            and re.search(r"\b(model|models|back to|normal|default|usual|standard)\b", t)):
        return Intent("model_set", {"agent": _agent(t), "model": "default"})

    # One stock's day, from the records.
    if tickers and (_STORY_Q.search(t) or t.upper() in tickers or t.upper() == tickers[0]):
        return Intent("ticker", {"tickers": tickers})

    if _TRADES.search(t):
        return Intent("trades")
    if _PNL.search(t):
        return Intent("pnl")
    if _POSITIONS.search(t) and not _RULES_Q.search(t):
        return Intent("positions")
    if _TASKS.search(t):
        return Intent("tasks")
    if _STATUS.search(t):
        return Intent("status")
    if _HOURS.search(t):
        return Intent("hours")
    if _TODAY.search(t):
        return Intent("today")
    if _WHOAMI.search(t):
        return Intent("whoami")
    return None


# The answer to "which one?" after "use opus" named no agent.
_ANSWER_AGENT = re.compile(
    r"^(?:the |for the |on the )?(reader|decider|both|all|either|each|them|both of them|all "
    r"of them)(?: one| of them| please)?$"
)


def answer_agent(text: str) -> str | None:
    """'the decider' / 'both' as the answer to 'For the reader or the decider?'."""
    m = _ANSWER_AGENT.match(normalise(text))
    if not m:
        return None
    w = m.group(1)
    return "reader" if w == "reader" else "decider" if w == "decider" else "both"


# Words that make a question one about the day's trading, so the decider is handed the
# records with it (asxbot.chat.facts_for).
_TRADING_TALK = re.compile(
    r"\b(?:today|yesterday|this morning|this afternoon|trade[ds]?|trading|bought|buy|sell|"
    r"sold|short|shorted|cover|position|positions|fill|filled|fills|order|orders|stop|stops|"
    r"target|pass|passed|skip|skipped|reject|rejected|took|decision|decided|p ?& ?l|pnl|"
    r"profit|loss|made|lost|account|accounts|balance|equity|setup|setups|announcement|"
    r"announcements|reaction|scan|screen|screened|watcher|arena|bot|rule|day trader|"
    r"daytrader|v2|why|how did|what happened|what did)\b"
)


def about_trading(text: str) -> bool:
    return bool(_TRADING_TALK.search(normalise(text)))


def step_level(current: str, direction: str) -> str | None:
    """'think harder' from the current level: the next step on the ladder, or None when the
    current level is not on it (off, adaptive, unknown)."""
    cur = str(current or "").lower()
    if cur == "off" and direction == "up":
        return "minimal"
    if cur not in THINK_LADDER:
        return None
    i = THINK_LADDER.index(cur) + (1 if direction == "up" else -1)
    return THINK_LADDER[max(0, min(i, len(THINK_LADDER) - 1))]
