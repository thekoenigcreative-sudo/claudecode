"""Plain language for the Trader chat: what Rick means, worked out in code.

Rick, 25 Sep 2026: "i need to be able to just tell it things without commands". Every command
the chat has (/status, /stop, /model, /think, /queue, /new, /reset, /change, /undo, /changes,
/help, /positions) also works from an ordinary sentence - "how's it going today", "what did
it trade", "use opus for the decider", "why did it pass on NWL", "show me the positions" -
and a question about the day's trading is answered from the arena's records (arena/today.py),
never guessed. The commands stay as shortcuts.

`understand(text, known_codes, recent)` turns a message into an Intent or None. It is plain
pattern matching: no model reads Rick's words here, so what a sentence does is decided in code
and testable line by line (tests/test_plain.py). None means the message is not one of the
things the chat does itself: it goes on to the change-request check and then to the decider
as a conversation, as before.

26 Sep 2026 (a review of the chat, tests/test_chat_review.py):
- A model or thinking change is only ever an INSTRUCTION: a sentence that starts with a verb
  ("use opus for the decider") or is nothing but the setting ("decider back to normal",
  "reader effort low"). A "?" anywhere, a negation ("don't switch the decider to sonnet") or
  a hedge ("maybe opus for the reader", "... tomorrow") only shows the models. The old rule
  that any message of six words or fewer was an instruction is gone, and even an
  instruction is only read back to Rick with buttons in the chat, never applied at once.
- "No new entries today" is a real switch now (arena/pause.py): "stop it for today",
  "no new entries today", "kill switch", "don't trade today" are `pause`; "resume trading"
  is `resume`. A bare "stop" still drops the answer being worked on.
- A lower-case word is a stock code only after on/with/about/in/for/of (or in a list after
  one), or when the day's records name it: "what happened today pls" is not about PLS.
- "whats", "hows", "didnt", "c'mon", "fuck it ..." are read as what Rick means.
- IB Gateway and the data feed (`feed`), the evening report (`report_sent`), a watcher
  restart (`watcher_restart`) and this week (`week`) have their own answers.
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
    r"trader|bot|fuck it|fuck|ffs|jesus|christ|ugh|oh|wtf|c'mon|cmon|come on)[,!.\s]+)*"
    r"(?:(?:can|could|would|will|pls|please)\s+you\s+(?:please\s+)?(?:just\s+)?)?"
    r"(?:(?:i(?:'d| would)? (?:like|want) (?:you )?to|i need (?:you )?to|let'?s|"
    r"go ahead and|just|quickly|please)\s+)?",
    re.I,
)
_SUFFIX = re.compile(r"(?:[\s,]+(?:please|pls|thanks|thank you|cheers|mate|ta))*[\s?!.]*$",
                     re.I)  # fmt: skip
# Rick types fast (26 Sep 2026): "whats running", "hows it goin", "didnt get the report".
# Read them as the apostrophe forms every pattern below is written for.
_SPELLINGS = [
    (re.compile(r"\bwhats\b"), "what's"), (re.compile(r"\bhows\b"), "how's"),
    (re.compile(r"\bwheres\b"), "where's"), (re.compile(r"\bwhos\b"), "who's"),
    (re.compile(r"\bthats\b"), "that's"), (re.compile(r"\blets\b"), "let's"),
    (re.compile(r"\b(?:wat|wot)\b"), "what"),
    (re.compile(r"\b(did|do|does|is|has|have|had|was|were|are|should|would|could)nt\b"),
     r"\1n't"),
    (re.compile(r"\bcant\b"), "can't"), (re.compile(r"\bwont\b"), "won't"),
    (re.compile(r"\bim\b"), "i'm"), (re.compile(r"\bive\b"), "i've"),
    (re.compile(r"\br u\b"), "are you"), (re.compile(r"\bu\b"), "you"),
    (re.compile(r"\b(go|do|look|track|trad|runn)in\b"), r"\1ing"),
    (re.compile(r"\b(?:the fuck|fucking|fuckin|bloody)\s+"), ""),
]  # fmt: skip


# The same politeness, but a code in capitals at the end stays: "why did it pass on PLS".
_SUFFIX_CASED = re.compile(r"(?:[\s,]+(?:[Pp]lease|[Pp]ls|[Tt]hanks|[Tt]hank you|[Cc]heers|"
                           r"[Mm]ate|[Tt]a))*[\s?!.]*$")  # fmt: skip


def trimmed(text: str) -> str:
    """Straight quotes, one space, and the politeness trimmed off both ends, in Rick's own
    case (stock codes are read from this: 'what happened today pls' ends at 'today')."""
    t = (text or "").replace("’", "'").replace("‘", "'").strip()
    t = re.sub(r"\s+", " ", t)
    t = _SUFFIX_CASED.sub("", t)
    t = _PREFIX.sub("", t)
    return t.strip()


def normalise(text: str) -> str:
    """Lower case, straight quotes, one space, the politeness trimmed off both ends and the
    quick spellings read out: 'Hey, could you please show me the positions?' -> 'show me the
    positions'; 'whats running' -> "what's running"."""
    t = (text or "").replace("’", "'").replace("‘", "'").strip()
    t = re.sub(r"\s+", " ", t).lower()
    for pat, repl in _SPELLINGS:
        t = pat.sub(repl, t)
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
# word is only a stock when it is not one of these; in capitals (ALL) it is. The second
# block, 26 Sep 2026: words Rick uses that are listed codes ("what happened today pls").
_COMMON = set("""
about above add after again ago all also and any are ask back bad bar bars been best big
bit bot both but buy can cap car cash cut day did dip dog dot down due each end even ever
far fee few fix flat for from fun gap get give got had has have hey him his hit hold hot how
its job just keep key kid last late led let lie log lot low man map max may men mid mix mod
net new nil nod not now odd off oil old one only opt our out own pay per pet pin pit pop pot
pre pro pub put ran raw red rid rip rod row rub run sad saw say see set she sir sit six sky
son sum sun tag tap tax tea ten the tie tin tip toe ton too top toy try two use van via war
was way web wed wet who why win wit won yes yet you zip
jan aug oct nov aim age art ace ice sea eye gas gem kit leg lit mom oak pen rim tee uni val
wax doc hub bus cat cup egg ion ore sol spa ant arc ash fin pls wow bet omg mad fri min
""".split())
# A lower-case code counts right after one of these ("any news on nwl", "what about hls").
_BEFORE_CODE = {"on", "with", "about", "in", "for", "of"}
_LIST_WORDS = {"and", "or", "&", "vs", "versus", "then"}


def tickers_in(text: str, known: Collection[str], recent: Collection[str] = ()) -> list[str]:
    """The ASX codes named in a sentence, in order. A code in capitals (NWL) counts when the
    directory knows it - unless the whole message is in capitals. A lower-case one (nwl)
    counts only when it is not an ordinary word AND it follows on/with/about/in/for/of (or
    another code in a list: "hls and reg"), or `recent` - the day's records - names it."""
    out: list[str] = []
    known_up = {str(k).upper() for k in known}
    recent_up = {str(k).upper() for k in recent}
    body = trimmed(text)
    words = [re.sub(r"'s$", "", w) for w in re.findall(r"[A-Za-z0-9&']+", body)]
    letters = [c for c in body if c.isalpha()]
    shouting = bool(letters) and all(c.isupper() for c in letters) and len(words) >= 3
    last = -9
    for i, w in enumerate(words):
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{1,4}", w):
            continue
        up = w.upper()
        if up in _NOT_CODES or up not in known_up or up in out:
            continue
        if w == up and not shouting:
            out.append(up)
            last = i
            continue
        if w.lower() in _COMMON:
            continue
        prev = words[i - 1].lower() if i else ""
        listed = last == i - 1 or (prev in _LIST_WORDS and last == i - 2)
        if prev in _BEFORE_CODE or up in recent_up or listed:
            out.append(up)
            last = i
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
# The words that name the effort setting itself ("reason" and "thought" are too common).
_EFFORT = re.compile(r"\b(?:think|thinking|effort|reasoning)\b")
_QUESTION = re.compile(r"^(?:what|which|who|why|how|is|are|does|do|has|have|did|was|were|"
                       r"should|would|could|will|can it|can you tell|tell me)\b")  # fmt: skip
# A setting is changed only by a sentence shaped as an instruction ("use opus for the
# decider", "make the reader think harder") or one that is nothing but the setting ("decider
# back to normal", "reader effort low", "think less"): a remark that merely mentions a model
# or a level ("the decider used opus yesterday and passed") is conversation, never a strategy
# change. 26 Sep 2026: the old "six words or fewer is an instruction" rule is gone - it set
# models from "sonnet for the reader?" and "the decider stays on opus".
_SET_VERB = re.compile(r"^(?:set|put|make|turn|dial|crank|switch|change|let|have|get|bump|"
                       r"raise|lower|increase|decrease|reduce|drop|move|give|use|run|go|try|"
                       r"i (?:want|need|would like|'d like)|let'?s|from now on)\b")  # fmt: skip
_WHO = (r"(?:the )?(?:reader|decider|both|both of them|them both|both agents)(?:'s)?")
_TERSE_MODEL = re.compile(
    rf"^(?:{_WHO}(?: model)?(?: (?:to|on|onto|back to|back on))? )?"
    r"(?:opus|sonnet|haiku)(?:[\s-]*\d+(?:[.\-]\d+)?)?"
    r"(?: (?:for|on) (?:the )?(?:reader|decider|both|both of them))?(?: now| instead)?$"
)
_TERSE_DEFAULT = re.compile(
    rf"^{_WHO}(?: model| models| thinking| effort)? back to (?:normal|default|usual|standard|"
    r"how it was|its usual|the default)$"
)  # fmt: skip
_TERSE_THINK = re.compile(
    rf"^(?:{_WHO} )?(?:think|thinking|effort|reasoning)(?: level)?(?: to| at)? (?:harder|more|"
    r"less|deeper|lower|higher|up|down|off|minimal|low|medium|high|x-?high|extra[ -]high|max|"
    r"maximum|adaptive)$"
)  # fmt: skip
# Never an instruction to change a setting, whatever else the sentence says.
_NEGATION = re.compile(r"\b(?:don't|do not|never|not|no need|shouldn't|should not|won't|"
                       r"will not|no longer|stays?|staying|keep|leave|without)\b")  # fmt: skip
_HEDGE = re.compile(r"\b(?:maybe|perhaps|reckon|should|shall|what if|tomorrow|later|after "
                    r"the test|next week|one day|at some point|eventually|might|wonder|"
                    r"thinking (?:of|about)|consider|considering|idea)\b")  # fmt: skip


def _instruction(t: str) -> bool:
    if _QUESTION.match(t):
        return False
    return bool(_SET_VERB.match(t) or _TERSE_MODEL.match(t) or _TERSE_DEFAULT.match(t)
                or _TERSE_THINK.match(t))  # fmt: skip


# A real order instruction: an order verb first, and something to trade - a stock, the
# positions, everything, a number. "short answer please", "cover the basics", "add to the
# list", "exit" and "open a new chat" are not orders (26 Sep 2026).
_ORDER_VERB = re.compile(
    r"^(?:close(?: out)?|sell|buy|short|cover|exit|dump|get out of|get into|place|enter|"
    r"go long|go short|take profits?(?: on)?|add to|double (?:down|up)(?: on)?|flatten|"
    r"liquidate|unwind|bail on|put on|open (?:a |an |the |another |new )+(?:position|trade|"
    r"long|short))\b(?! (?:the |a )?(?:question|note|look))"
)
_ORDER_OBJECT = re.compile(
    r"\b(?:position|positions|everything|it all|all of it|all|the lot|shares?|stock|stocks|"
    r"holdings?|longs?|shorts?|trade|trades|order|orders|book|now|them|those|these|it)\b|\$|\d"
)
# "would you buy PLS" asks the decider's opinion; "can you sell BHP" is an instruction.
_OPINION = re.compile(r"^(?:would|should|shall|do you think|what if|how about|is it worth)\b")
_STOP = re.compile(
    r"^(?:stop|cancel|abort|halt|kill|never ?mind|nevermind|forget (?:it|that)|leave it|"
    r"hold on|hang on|drop (?:it|that))"
    r"(?:\s+(?:it|that|this|now|please|the answer|what you'?re doing|working|working on "
    r"(?:it|that|this)|answering|talking|thinking|there))*$"
)
# Rick's "no new entries today" (arena/pause.py, 26 Sep 2026): trading words, or "it" /
# "everything" with the day. A bare "stop" or "stop it" is still the answer being worked on.
_DAY_WORDS = (r"(?:\s+(?:for (?:today|now|the day|the rest of (?:the day|today))|today|now|"
              r"right now|immediately|until tomorrow|for a bit|for a while|then|mate|"
              r"straight away|asap))*")  # fmt: skip
_PAUSE = re.compile(
    r"^(?:make it |have it |get it |i want (?:it|you) to |tell it to )?(?:"
    r"(?:stop|pause|halt|suspend|freeze|quit|cease)\s+(?:all |any )?(?:new )?(?:trading|"
    r"trades|entries|entering|buying|opening (?:new |any )?(?:positions|trades)|taking (?:new "
    r"|any )?(?:trades|positions)|placing (?:new |any )?(?:trades|orders)|new (?:trades|"
    r"positions|entries)|it trading|the (?:bot|bots|trader|arena|watcher|trading)|"
    r"everything|it all|all trading|all trades)"
    r"|(?:switch|turn|shut|power)\s+(?:it|everything|the (?:bot|trader|arena|watcher|"
    r"trading))\s+(?:off|down)"
    r"|(?:switch|turn|shut|power)\s+(?:off|down)(?:\s+(?:the (?:bot|trader|arena|watcher)|"
    r"trading|everything))?"
    r"|kill switch|(?:hit |pull )?(?:the )?kill switch|emergency stop|panic button|pull the plug"
    r"|no (?:more )?(?:new )?(?:entries|trades|trading|positions|buys|buying)"
    r"|(?:don't|do not) (?:open|take|enter|make|place|start) (?:any )?(?:more |new |other )*"
    r"(?:positions?|trades?|entries)"
    r"|(?:don't|do not) (?:trade|buy|enter)(?: any ?more| anything(?: else)?| again)?"
    r"|that's enough (?:trading )?(?:for today|today)|take (?:the rest of )?(?:the day|today) off"
    r"|call it a day|pack it in|stand down"
    r")" + _DAY_WORDS + r"$"
)
_PAUSE_DAY = re.compile(
    r"^(?:make it |have it )?(?:stop|pause|halt|kill|freeze|shut)(?: it| that| everything| it "
    r"all)?\s+(?:for (?:today|the day|the rest of (?:the day|today))|today|until tomorrow)"
    + _DAY_WORDS + r"$"
)
_RESUME = re.compile(
    r"^(?:(?:resume|unpause|un-pause|re-?enable|restore)(?: (?:trading|entries|new entries|"
    r"buying|the (?:bot|trader|arena|trading)|it))?"
    r"|(?:start|begin) (?:trading|buying|taking (?:new )?trades|entering)(?: again)?"
    r"|(?:allow|enable) (?:new )?(?:entries|trades|trading|positions)(?: again)?"
    r"|let it (?:trade|buy|enter)(?: again)?|(?:turn|switch) (?:it|trading|the (?:bot|trader)) "
    r"back on|(?:back to|carry on|go back to) trading|trade again|lift the pause)"
    r"(?:\s+(?:again|today|now|please|for today|for the rest of (?:the day|today)))*$"
)
_WATCHER_RESTART = re.compile(
    r"^(?:(?:re)?start|reboot|bounce|kick|relaunch|boot)(?: up)? (?:the )?(?:watcher|bot|"
    r"trader|arena)(?: (?:again|now|up|back up))*$"
    r"|\b(?:restart|reboot|relaunch|bounce) (?:the )?watcher\b"
    r"|\b(?:can|could) (?:you|it|the watcher|we) (?:be )?(?:restart(?:ed)?|reboot(?:ed)?)\b"
)
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
    r"open (?:a )?(?:new|fresh) (?:chat|conversation|session|thread)|"
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
# ... but "what changes did it make to the stops" is about the trading, not change requests.
_BOT_CHANGES = re.compile(
    r"\bchanges? (?:did|has|have|does|do|will|would) (?:it|the bot|the agent|the decider|they|"
    r"the watcher|the rule bot) (?:make|made|do)\b|\bchanges? (?:to|on|in|of) (?:the |its |"
    r"my |our )?(?:stops?|targets?|positions?|trades?|orders?|sizes?|sizing|trailing)\b"
)
_QUEUE_SHOW = re.compile(
    r"\b(?:queue (?:mode|setting|settings|status|is)|what happens (?:if|when) i (?:message|"
    r"send|text|write|say)|while (?:you'?re|it'?s|you are|it is) busy|how do you (?:handle|"
    r"deal with) (?:messages|it|things) (?:when|while)|what'?s the queue|how'?s the queue)\b"
)
# Only an explicit queue instruction sets the mode (26 Sep 2026: "sorry to interrupt" and
# "take it one at a time" did).
_QUEUE_SET = [
    (re.compile(r"\b(?:answer (?:them |my messages |messages |each one |each |everything )?"
                r"(?:one at a time|in turn|in order|one by one|each in turn)|follow ?up mode|"
                r"queue (?:mode )?(?:to )?follow-?up)\b"), "followup"),  # fmt: skip
    (re.compile(r"\b(?:bundle|collect|batch|group|combine|gather) (?:up )?(?:all )?(?:my |the )?"
                r"messages\b|\bcollect mode\b"), "collect"),  # fmt: skip
    (re.compile(r"^(?:interrupt|interrupt mode|use interrupt mode|switch to interrupt(?: mode)?)$"
                r"|\bqueue (?:mode )?(?:to )?interrupt\b"), "interrupt"),  # fmt: skip
    (re.compile(r"\bsteer mode\b|\bqueue (?:mode )?(?:to )?steer\b"), "steer"),
    (re.compile(r"\bqueue\b.*\b(?:default|normal|back to)\b|\b(?:default|normal) queue\b"),
     "default"),  # fmt: skip
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
    r"trading (?:today|so far|log|activity)|traded (?:today|anything|yet)|why no trades)\b"
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
    r"returns?"
    # 26 Sep 2026: money in Rick's words, and the agent against its rule bot
    r"|how much (?:money )?(?:did|have|has|are|is) (?:we|it|you|they|the agent|the bot) "
    r"(?:make|made|making|lose|lost|losing)|(?:did|have|has) (?:we|it|you) (?:make|made|lose|"
    r"lost) (?:any )?(?:money|anything|a profit|a loss)|(?:make|made|making|lose|lost|losing) "
    r"(?:any |some |much )?money|the damage|what(?:'s| is) the total|total (?:so far|p ?& ?l|"
    r"pnl|profit|result|return)|in total"
    r"|(?:bot|bots|agent|agents) (?:vs\.?|versus|v|or|against) (?:the )?(?:bot|bots|agent|"
    r"agents)|^(?:vs\.?|versus) (?:the )?(?:bot|bots|agent|agents)$|(?:is|are) the (?:agent|"
    r"agents|bot|bots|ai) (?:beating|ahead of|behind|winning|losing|doing better|doing worse|"
    r"outperforming|up on)|who(?:'s| is) (?:winning|ahead|in front|leading))\b"
)
_POSITIONS = re.compile(
    r"\b(?:positions?|holdings?|what(?:'s| is) open|what (?:are|do|does|is|am) (?:we|you|it|"
    r"the bot|the agent|i) (?:hold|holding|have|own|got|have on|in|long|short|carrying)|"
    r"what(?:'s| is) in the (?:book|books|portfolio|accounts?)|portfolio|exposure|(?:anything|"
    r"what|something) (?:still )?open|open (?:trades|book)|show me the book|the book|what "
    r"(?:are we|is it|am i) (?:in|long|short)|what have we got on|still in (?:anything|"
    r"something))\b"
)
# "reduce the position size" is about sizing, not the open positions.
_SIZING = re.compile(r"\bposition (?:size|sizes|sizing)\b|\bsize of (?:a |the |each )?position")
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
    r"|^(?:the )?(?:watcher|bot|arena|trader)(?: still)? (?:up|running|alive|on|ok|okay|"
    r"working|down|dead|going|there)$"
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
    r"day|your day)|how(?:'s| is) (?:my|the) (?:bot|trader) (?:doing|going)"
    r"|how(?:'s| is) (?:the )?(?:day ?trader|announcements(?: playbook)?|v2|rule bot) "
    r"(?:going|doing|looking|tracking))\b"
)
_HOURS = re.compile(
    r"\b(?:when (?:does|do|did|will|is|are|should) (?:it|the watcher|trading|the arena|the "
    r"bot|the market|the report|the evening report|you|the day) (?:start|stop|run|open|"
    r"close|finish|end|go|come|due|arrive|sent|kick off|knock off|begin|wake up|shut down)|"
    r"what time (?:does|do|will|is|did)|(?:trading|watcher|market|arena|its|the|your|"
    r"opening|operating|working) hours|what (?:are|is) (?:the|its|your) (?:hours|schedule|"
    r"timetable|window|times)|when (?:is|does|will) the (?:report|evening report)|daylight "
    r"saving|dst|what time (?:is|does) (?:the|it)"
    r"|is (?:the )?(?:market|asx|sharemarket|stock market|exchange) (?:open|closed|shut|"
    r"trading)|(?:is|was|will) (?:today|it|tomorrow|monday|tuesday|wednesday|thursday|friday)"
    r"(?: be)? an? (?:trading|market|asx) day|trading day (?:today|tomorrow)|market open "
    r"(?:today|now|yet))\b"
)
_REPORT_SENT = re.compile(
    r"\b(?:(?:did|has|have|was) (?:the |tonight's |today's |last night's |my |an? )?(?:evening "
    r")?report (?:go out|gone out|been sent|sent|arrive|arrived|come|come through|been|go|"
    r"get sent)|(?:didn't|did not|haven't|have not|never|don't) (?:get|got|receive|received|"
    r"see|seen) (?:the |an? |tonight's |my |today's |last night's )?(?:evening )?report"
    r"|(?:where(?:'s| is)|no) (?:the |my |tonight's )?(?:evening )?report|(?:evening )?report "
    r"(?:sent|went out|arrived|didn't come|never came)|(?:was|has) (?:the |tonight's )?"
    r"(?:evening )?report sent)\b"
)
# IB Gateway and the prices (26 Sep 2026: "gateway should be back" was answered about
# OpenClaw's gateway). In this chat "gateway" is IB Gateway.
_FEED = re.compile(
    r"\b(?:ibkr|ib gateway|interactive brokers|gateway|yahoo|connection doctor)\b"
    r"|\b(?:data|prices?|feed|quotes?|stream|streams)(?: is| are)? (?:live|real[- ]?time|"
    r"delayed|stale|down|up|working|flowing|ok|okay|back|frozen|coming through)\b"
    r"|\b(?:live|real[- ]?time|delayed) (?:data|prices?|feed|quotes?|bars)\b"
    r"|\b(?:the |data )feed\b|\bentries (?:paused|allowed|blocked)\b"
)
_WEEK = re.compile(r"\b(?:this|last|the|past|previous) week\b|\bweek so far\b|\bweek to date\b")
_WEEK_Q = re.compile(r"\b(?:how|what|did|any|show|results?|summary|recap|went|go|going|"
                     r"trades?|traded|fills?|money|up|down|made|lost|p ?& ?l|pnl)\b")  # fmt: skip
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


def _order_request(raw: str, t: str, tickers: list[str]) -> bool:
    low = " ".join(raw.lower().replace("’", "'").split())
    if _OPINION.match(low) or not _ORDER_VERB.match(t) or _STOP.match(t):
        return False
    if tickers or _ORDER_OBJECT.search(_ORDER_VERB.sub("", t, count=1)):
        return True
    # "sell XYZ" with a code the directory does not know is still an order instruction: it is
    # refused in code, never handed to the decider (26 Sep 2026).
    return bool(re.search(r"\b[A-Z][A-Z0-9]{1,4}\b", raw.replace("I ", " ")))


def understand(text: str, known_codes: Collection[str] = (),
               recent: Collection[str] = ()) -> Intent | None:  # fmt: skip
    """What Rick's message asks the chat to do itself, or None (a change request, or a
    conversation with the decider). Checked in this order, most specific first. `recent`
    is the day's record codes (a lower-case code they name counts as a stock)."""
    raw = (text or "").strip()
    if not raw or raw.startswith("/"):
        return None
    t = normalise(raw)
    if not t:
        return None
    tickers = tickers_in(raw, known_codes, recent)
    asked = "?" in raw

    if _order_request(raw, t, tickers):
        return Intent("order_request", {"tickers": tickers})
    if _PAUSE.match(t) or _PAUSE_DAY.match(t):
        stops = bool(re.match(r"(?:make it |have it )?(?:stop|halt|kill|cancel|abort)\b", t))
        return Intent("pause", {"stop_answer": stops, "watcher": "watcher" in t})
    if _RESUME.match(t):
        return Intent("resume")
    if _STOP.match(t):
        return Intent("stop", {"trading": False})
    if _WATCHER_RESTART.search(t):
        return Intent("watcher_restart")
    if _HELP.match(t):
        return Intent("help")
    if _NEW.match(t):
        return Intent("new", {"reset": t.startswith(("reset", "clear", "wipe", "clean"))})
    if _UNDO.match(t):
        return Intent("undo")
    if _CHANGES.search(t) and not _BOT_CHANGES.search(t):
        return Intent("changes")
    if not asked and not _QUESTION.match(t):
        for pat, mode in _QUEUE_SET:
            m = pat.search(t)
            if m:
                return Intent("queue_set", {"mode": mode})
    if _QUEUE_SHOW.search(t):
        return Intent("queue_show")

    # The agents' models and effort levels. A question, a negation or a hedge only shows
    # them; only an instruction changes one, and the chat reads that back before it does.
    has_model = _MODEL.search(t) is not None
    musing = asked or bool(_NEGATION.search(t) or _HEDGE.search(t))
    if _THINK_SHOW.search(t):
        return Intent("think_show")
    if _MODEL_SHOW.search(t) or (has_model and (_QUESTION.match(t) or musing)):
        return Intent("model_show")
    if _EFFORT.search(t) and musing and (_agent(t) or _level_value(t)):
        return Intent("think_show")
    instruction = _instruction(t) and not musing
    if _THINK.search(t) and instruction:
        level = _level_value(t)
        if level:
            return Intent("think_set", {"agent": _agent(t), "level": level})
    if has_model and instruction:
        return Intent("model_set", {"agent": _agent(t), "model": _model_value(t)})
    if (_DEFAULT.search(t) and _agent(t) and instruction
            and re.search(r"\b(model|models|back to|normal|default|usual|standard)\b", t)):
        return Intent("model_set", {"agent": _agent(t), "model": "default"})

    if _FEED.search(t) and not tickers:
        return Intent("feed")
    if _REPORT_SENT.search(t):
        return Intent("report_sent")

    # One stock's day, from the records.
    if tickers and (_STORY_Q.search(t) or t.upper() in tickers or t.upper() == tickers[0]):
        return Intent("ticker", {"tickers": tickers})

    if _WEEK.search(t) and _WEEK_Q.search(t):
        return Intent("week", {"which": "last" if re.search(r"\b(?:last|previous) week\b", t)
                               else "this"})  # fmt: skip
    if _TRADES.search(t):
        return Intent("trades")
    if _PNL.search(t):
        return Intent("pnl")
    if _POSITIONS.search(t) and not _RULES_Q.search(t) and not _SIZING.search(t):
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


# A plain yes or no to a read-back the chat has just sent (a pause, a model change).
_YES = re.compile(r"^(?:yes|yep|yeah|yup|y|ok|okay|sure|do it|go ahead|confirm|confirmed|"
                  r"please do|yes do it|yes please|go for it|yes go ahead|"
                  r"change it anyway)$")  # fmt: skip
_NO = re.compile(r"^(?:no|nope|nah|n|cancel|cancel that|don't|do not|leave it|never ?mind|"
                 r"forget it|no thanks|not now|no don't)$")  # fmt: skip


def yes_no(text: str) -> str | None:
    """'yes' / 'no' when a message is only that, else None."""
    t = normalise(text)
    if _YES.match(t):
        return "yes"
    if _NO.match(t):
        return "no"
    return None


# Words that make a question one about the day's trading, so the decider is handed the
# records with it (asxbot.chat.facts_for). 26 Sep 2026: money, the week, the feed and the
# report too ("did we lose money", "is the gateway back").
_TRADING_TALK = re.compile(
    r"\b(?:today|yesterday|this morning|this afternoon|trade[ds]?|trading|bought|buy|sell|"
    r"sold|short|shorted|cover|position|positions|fill|filled|fills|order|orders|stop|stops|"
    r"target|pass|passed|skip|skipped|reject|rejected|took|decision|decided|p ?& ?l|pnl|"
    r"profit|loss|made|lost|account|accounts|balance|equity|setup|setups|announcement|"
    r"announcements|reaction|scan|screen|screened|watcher|arena|bot|rule|day trader|"
    r"daytrader|v2|why|how did|what happened|what did|money|make|lose|total|pending|working|"
    r"week|ibkr|gateway|feed|data|report|replay|entries|paused)\b"
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
