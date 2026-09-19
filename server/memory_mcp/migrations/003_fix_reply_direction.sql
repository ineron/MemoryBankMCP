-- Fix task #4: message_send flipped direction wrongly when a project
-- replied to its OWN prior message in a thread (continuing it), rather
-- than to the other side's message.
--
-- Root cause was two layers deep:
--   1. messaging.py's send() derived a reply's from/to by unconditionally
--      swapping the immediate parent's from/to. Correct when the parent is
--      the OTHER side's message, wrong when the parent is your own -- the
--      second swap flips it back to the wrong direction. Fixed in
--      messaging.py by requiring `from_project` on every reply and
--      deriving direction from it instead of guessing from the parent.
--   2. This DB trigger enforced the same wrong assumption at a deeper
--      layer, independent of the Python fix: it required every reply's
--      to_project to equal the parent's from_project_id, unconditionally
--      -- which rejects a valid "continuing my own message" reply outright
--      (that reply's to_project must stay the same as the parent's
--      to_project, not flip to the parent's from_project). This migration
--      relaxes the check to the two valid directions between the same
--      two participants.
--
-- Repro (thread 199, pg_ipl <-> ledgyx-admin-ui, 2026-08-14/15): pg_ipl
-- replied to its own message 200 (continuing the thread) and the message
-- came out attributed as if ledgyx-admin-ui had sent it.

CREATE OR REPLACE FUNCTION messages_set_thread() RETURNS TRIGGER AS $$
DECLARE
    parent messages%ROWTYPE;
BEGIN
    IF NEW.in_reply_to IS NULL THEN
        IF NEW.kind = 'reply' THEN
            RAISE EXCEPTION 'kind=reply requires in_reply_to to be set';
        END IF;
        NEW.thread_id   := NEW.id;
        NEW.reply_depth := 0;
    ELSE
        SELECT * INTO parent FROM messages WHERE id = NEW.in_reply_to;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'in_reply_to=% does not exist', NEW.in_reply_to;
        END IF;
        NEW.thread_id   := parent.thread_id;
        NEW.reply_depth := parent.reply_depth + 1;
        -- A reply must run between the same two projects already in this
        -- conversation, in either direction: the other side replying back
        -- (from/to flip relative to the parent), or a project continuing
        -- its OWN prior message in the thread (from/to unchanged from the
        -- parent).
        IF parent.from_project_id IS NOT NULL THEN
            IF NOT (
                (NEW.from_project_id = parent.to_project_id
                 AND NEW.to_project_id = parent.from_project_id)
                OR
                (NEW.from_project_id = parent.from_project_id
                 AND NEW.to_project_id = parent.to_project_id)
            ) THEN
                RAISE EXCEPTION
                    'reply to message % must be between projects % and % (its participants), got from=% to=%',
                    parent.id, parent.from_project_id, parent.to_project_id,
                    NEW.from_project_id, NEW.to_project_id;
            END IF;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
