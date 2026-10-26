"""Message text per event. Placeholders use str.format names from the context.

Messages carry the event and a link, never marks or document contents (M17-F13).
Add a language by adding a key to TEMPLATES ("hi" for Hindi); missing entries
fall back to English.
"""
from app.enums import NotificationEvent as E

TEMPLATES = {
    "en": {
        E.REGISTRATION_PENDING_APPROVAL: ("Registration awaiting your approval",
                                          "{requester} has registered as admin of {institution}. Please review the request."),
        E.REGISTRATION_DECIDED: ("Your registration was {decision}",
                                 "Your admin registration for {institution} was {decision}. {note}"),
        E.ACCESS_REQUEST_PENDING: ("Access request awaiting your approval",
                                   "{requester} has requested staff access to {institution}."),
        E.ACCESS_REQUEST_DECIDED: ("Your access request was {decision}",
                                   "Your access request for {institution} was {decision}. {note}"),
        E.INVITATION_SENT: ("You are invited to {institution}",
                            "You have been invited to join {institution} as {role}. Use this link within {days} days: {link}"),
        E.INVITATION_EXPIRING: ("Your invitation expires soon",
                                "Your invitation to {institution} expires on {expires_on}."),
        E.SECOND_ADMIN_CLAIM: ("New admin claim for {institution}",
                               "{requester} has asked to become an admin of {institution}. The platform team will review it."),
        E.OTP: ("Your verification code", "Your code is {otp}. It expires in {minutes} minutes. Do not share it."),
        E.PASSWORD_CHANGED: ("Your password was changed",
                             "Your password was changed on {when}. If this was not you, reset it now."),
        E.PASSWORD_RESET: ("Reset your password",
                           "Use this link within {minutes} minutes to reset your password: {link}"),
        E.ACCOUNT_LOCKED: ("Your account is temporarily locked",
                           "Too many failed logins. Your account is locked until {until}."),
        E.ENROLMENT_VERIFICATION_REQUESTED: ("Enrolment verification requested",
                                             "{student} has asked {institution} to verify their enrolment."),
        E.ENROLMENT_VERIFICATION_DECIDED: ("Your enrolment was {decision}",
                                           "{institution} has {decision} your enrolment. {note}"),
        E.COLLEGE_ONBOARDING_REQUESTED: ("A student asked us to onboard {institution}",
                                         "{student} asked for {institution} to join the platform."),
        E.REQUEST_SUBMITTED: ("Migration request {reference} submitted",
                              "Your migration request {reference} to {destination} was submitted."),
        E.NOC_REQUESTED: ("NOC requested for {reference}",
                          "{student} has applied to move to {destination}. Please decide the NOC by {due}."),
        E.NOC_DECIDED: ("NOC {decision} for {reference}", "Your current college has {decision} the NOC. {note}"),
        E.NOC_ESCALATED: ("NOC escalated for {reference}",
                          "{source} did not decide the NOC for {reference} in time. It is now with you."),
        E.RETURNED_FOR_CORRECTION: ("Corrections needed on {reference}",
                                    "{destination} has asked for corrections: {note}"),
        E.DOCUMENT_DECIDED: ("Document {decision}", "Your {document} was {decision}. {note}"),
        E.INTERVIEW_SCHEDULED: ("Interview scheduled for {reference}",
                                "Your interview with {college} is on {when} ({mode}). Join or report at: {where} {info}"),
        E.INTERVIEW_RESCHEDULED: ("Interview rescheduled for {reference}",
                                  "Your interview is now on {when} ({mode}). Join or report at: {where} {info}"),
        E.INTERVIEW_LINK_UPDATED: ("New meeting link for {reference}",
                                   "The meeting link for your interview on {when} has changed. Join at: {where} {info}"),
        E.INTERVIEW_CANCELLED: ("Interview cancelled for {reference}", "Your interview on {when} was cancelled. {note}"),
        E.INTERVIEW_REMINDER: ("Interview reminder",
                               "Reminder: your interview for {reference} starts at {when}. Join or report at: {where} {info}"),
        E.NO_SHOW_RECORDED: ("Missed interview recorded", "You did not attend the interview for {reference}. {note}"),
        E.RECOMMENDED: ("Request {reference} awaiting university approval",
                        "{destination} has recommended {reference}. Please decide entry approval."),
        E.WAITLISTED: ("You are on the waitlist", "Request {reference} is approved and waitlisted at position {rank}."),
        E.OFFER_ISSUED: ("Provisional offer for {reference}",
                         "You have a provisional offer from {destination}. Accept it by {expires_on}."),
        E.OFFER_EXPIRING: ("Your offer expires soon", "Your offer for {reference} expires on {expires_on}."),
        E.OFFER_ACCEPTED: ("Release requested for {reference}",
                           "{student} accepted an offer from {destination}. Please issue the release by {due}."),
        E.RELEASE_REQUESTED: ("Release requested for {reference}", "Please issue the release for {reference} by {due}."),
        E.RELEASE_OVERDUE: ("Release overdue for {reference}", "The release for {reference} was due on {due}."),
        E.ADMITTED: ("Admission confirmed for {reference}",
                     "Your admission to {destination} is confirmed. Report with originals by {due}."),
        E.ORIGINALS_DEADLINE: ("Report with your original documents",
                               "Please report to {destination} with original documents by {due}."),
        E.REQUEST_REJECTED: ("Request {reference} was not approved",
                             "Your request {reference} was rejected. Reason: {note}. You may appeal once within {days} days."),
        E.REQUEST_CANCELLED: ("Request {reference} cancelled", "Request {reference} was cancelled. {note}"),
        E.APPEAL_FILED: ("Appeal filed on {reference}", "{student} has appealed the rejection of {reference}. Decide by {due}."),
        E.APPEAL_DECIDED: ("Your appeal was {decision}", "Your appeal on {reference} was {decision}. {note}"),
        E.OFFERING_REQUEST_PENDING: ("Course offering request", "{college} has asked to offer {course}."),
        E.OFFERING_REQUEST_DECIDED: ("Course offering request {decision}", "Your request to offer {course} was {decision}. {note}"),
        E.SUPPORT_TICKET_RAISED: ("Support ticket {reference}", "{requester} raised ticket {reference}: {subject}"),
        E.SUPPORT_TICKET_ANSWERED: ("Reply on ticket {reference}", "There is a new reply on your ticket {reference}."),
        E.GENERIC: ("{subject}", "{message}"),
    },
    "hi": {},
}


class _SafeDict(dict):
    def __missing__(self, key):
        return ""


def render(event, context, language="en"):
    table = TEMPLATES.get(language) or {}
    subject, body = table.get(event) or TEMPLATES["en"][event]
    ctx = _SafeDict({k: ("" if v is None else v) for k, v in (context or {}).items()})
    return subject.format_map(ctx).strip(), " ".join(body.format_map(ctx).split())
