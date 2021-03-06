import vkapi
import time
import log
from thread_manager import thread_manager, timeline
from user_cache import user_cache
import config
import re
import random
import html

CONF_START = 2000000000

ignored_errors = {
    # (code, method): (message, can_retry)
    (900, 'messages.send'): ('Blacklisted', False),
    (7, 'messages.send'): ('Banned', True),
    (10, 'messages.send'): ('Unable to reply', True),
    (15, 'friends.delete'): None,
    (100, 'messages.markAsRead'): None,
    (113, 'users.get'): None,
    (100, 'messages.removeChatUser'): ('Unable to leave', False),
}

class vk_bot:

    delay_on_reply = config.get('vkbot.delay_on_reply', 'i')
    chars_per_second = config.get('vkbot.chars_per_second', 'i')
    same_user_interval = config.get('vkbot.same_user_interval', 'i')
    same_conf_interval = config.get('vkbot.same_conf_interval', 'i')
    typing_interval = 5
    forget_interval = config.get('vkbot.forget_interval', 'i')
    delay_on_first_reply = config.get('vkbot.delay_on_first_reply', 'i')

    def __init__(self, username='', password=''):
        self.api = vkapi.vk_api(username, password, ignored_errors=ignored_errors)
        self.api.initLongpoll()
        self.users = user_cache(self.api, 'sex,crop_photo,blacklisted,blacklisted_by_me')
        self.initSelf()
        self.guid = int(time.time() * 5)
        self.last_viewed_comment = 0
        self.good_conf = {}
        self.tm = thread_manager()
        self.last_message = {}  # peer_id: (id, time, text)
        self.last_message_id = 0
        self.whitelist = None
        self.bad_conf_title = lambda s: False
        self.admin = None

    def initSelf(self):
        self.users.clear()
        res = self.api.users.get(fields='contacts,relation')[0]
        self.self_id = res['id']
        self.phone = res.get('mobile_phone', '')
        self.name = (res['first_name'], res['last_name'])
        self.bf = res.get('relation_partner')
        log.info('My phone: ' + self.phone)

    def getSender(self, message):
        if 'chat_id' in message:
            return 2000000000 + message['chat_id']
        return message['user_id']

    def loadUsers(self, arr, key, clean=False):
        users = []
        for i in arr:
            try:
                users.append(key(i))
            except Exception:
                pass
        self.users.load(users, clean)

    def replyOne(self, message, gen_reply, method=None):
        if self.whitelist and self.getSender(message) not in self.whitelist:
            return
        if 'chat_id' in message:
            if not self.checkConf(message['chat_id']):
                self.deleteFriend(message['user_id'])
                return
        if self.tm.isBusy(self.getSender(message)):
            return

        message['_method'] = method
        try:
            ans = gen_reply(message)
        except Exception as e:
            ans = None
            log.error('local {}: {}'.format(e.__class__.__name__, str(e)), True)
            time.sleep(1)
        if ans:
            self.replyMessage(message, ans[0], ans[1])


    def replyAll(self, gen_reply, include_read=False):
        self.tm.gc()
        if include_read:
            log.info('Include read')
            self.users.gc()
            try:
                messages = self.api.messages.getDialogs(unread=(0 if self.whitelist else 1), count=(20 if self.whitelist else 200))['items'][::-1]
            except (KeyError, TypeError):
                # may sometimes happen because of friendship requests
                return
            self.loadUsers(messages, lambda x:x['message']['user_id'])
            with self.api.api_lock:
                for msg in sorted(messages, key=lambda msg:msg['message']['id']):
                    cur = msg['message']
                    if cur['out']:
                        continue
                    if self.last_message_id and cur['id'] > self.last_message_id:
                        continue
                    self.replyOne(cur, gen_reply, 'getDialogs')
                self.api.sync()

        else:
            messages = self.longpollMessages()
            self.loadUsers(messages, lambda x:x['user_id'])
            with self.api.api_lock:
                for cur in sorted(messages, key=lambda msg:msg['id']):
                    self.last_message_id = max(self.last_message_id, cur['id'])
                    self.replyOne(cur, gen_reply)
                self.api.sync()

    def longpollMessages(self):
        arr = self.api.getLongpoll()
        need_extra = []
        result = []
        for i in arr:
            if i[0] == 4:  # new message
                mid = i[1]
                sender = i[3]
                ts = i[4]
                text = i[6]
                opt = i[7]
                flags = i[2]
                if flags & 2:  # out
                    continue

                for i in range(1, 11):
                    if opt.get('attach{}_type'.format(i)) == 'photo':
                        del opt['attach{}_type'.format(i)]
                        del opt['attach{}'.format(i)]
                        text += '.. '
                if  not (set(opt) <= {'from', 'emoji'} or opt.get('attach1_type') == 'sticker'):
                    need_extra.append(str(mid))
                    continue
                msg = {'id': mid, 'date': ts, 'body': text, 'out': 0, '_method': ''}
                if opt.get('attach1_type') == 'sticker':
                    msg['body'] = ''

                if 'from' in opt:
                    msg['chat_id'] = sender - CONF_START
                    msg['user_id'] = int(opt['from'])
                else:
                    msg['user_id'] = sender
                result.append(msg)

        if need_extra:
            need_extra = ','.join(need_extra)
            for i in self.api.messages.getById(message_ids=need_extra)['items']:
                i['_method'] = 'getById'
                result.append(i)
        return result

    def sendMessage(self, to, msg, resend=False):
        if not self.good_conf.get(to, 1):
            return
        self.guid += 1
        if resend:
            return self.api.messages.send(peer_id=to, forward_messages=msg, random_id=self.guid)
        else:
            return self.api.messages.send(peer_id=to, message=msg, random_id=self.guid)

    # fast==1: no delay
    #       2: no markAsRead
    def replyMessage(self, message, answer, fast=0):
        sender = self.getSender(message)
        if 'id' in message and message['id'] <= self.last_message.get(sender, (0, 0))[0]:
            return

        if not answer:
            if sender not in self.last_message or time.time() - self.last_message[sender][1] > self.forget_interval:
                tl = timeline().sleep(self.delay_on_first_reply).do(lambda:self.api.messages.markAsRead(peer_id=sender))
                self.tm.run(sender, tl)
            else:
                self.api.messages.markAsRead.delayed(peer_id=sender)
            self.last_message[sender] = (self.last_message.get(sender, (0, 0))[0], time.time(), '')
            return

        typing_time = 0
        if (fast == 0 or fast == 2) and not answer.startswith('&#'):
            typing_time = len(answer) / self.chars_per_second

        resend = False
        if sender in self.last_message and self.last_message[sender][2].upper() == answer.upper():
            log.info('Resending')
            typing_time = 0
            resend = True

        def _send():
            try:
                if resend:
                    res = self.sendMessage(sender, self.last_message[sender][0], resend=True)
                else:
                    res = self.sendMessage(sender, answer)
                if res is None:
                    del self.users[sender]
                    return
                self.last_message[sender] = (res, 0 if fast == 1 else time.time(), answer)
            except Exception as e:
                log.error('thread {}: {}'.format(e.__class__.__name__, str(e)), True)

        cur_delay = (self.delay_on_reply - 1) * random.random() + 1
        send_time = cur_delay + typing_time
        user_delay = 0
        if sender in self.last_message and sender != self.admin:
            user_delay = self.last_message[sender][1] - time.time() + (self.same_user_interval if sender < 2000000000 else self.same_conf_interval)  # can be negative
        tl = timeline(max(send_time, user_delay))
        if sender not in self.last_message or time.time() - self.last_message[sender][1] > self.forget_interval:
            if fast == 0:
                tl.sleep(self.delay_on_first_reply)
                tl.do(lambda:self.api.messages.markAsRead(peer_id=sender))
        else:
            tl.sleep_until(send_time, (self.delay_on_reply - 1) * random.random() + 1)
            if fast == 0:
                tl.do(lambda:self.api.messages.markAsRead(peer_id=sender))

        tl.sleep(cur_delay)
        if typing_time:
            tl.do_every_for(self.typing_interval, lambda:self.api.messages.setActivity(type='typing', user_id=sender), typing_time)
        tl.do(_send)
        self.tm.run(sender, tl)

    def checkConf(self, cid):
        if cid + CONF_START in self.good_conf:
            return self.good_conf[cid + CONF_START]
        messages = self.api.messages.getHistory(chat_id=cid)['items']
        for i in messages:
            if i.get('action') == 'chat_create':
                self.leaveConf(cid)
                log.write('conf', str(i.get('user_id')) + ' ' + str(cid))
                return False
        title = self.api.messages.getChat(chat_id=cid).get('title', '')
        if self.bad_conf_title(title):
            self.leaveConf(cid)
            log.write('conf',  'conf ' + str(cid) + ' (name: {})'.format(title))
            return False
        self.good_conf[cid + CONF_START] = True
        return True

    def leaveConf(self, cid):
        log.info('Leaving conf ' + str(cid))
        self.good_conf[cid + CONF_START] = False
        return self.api.messages.removeChatUser(chat_id=cid, user_id=self.self_id)

    def addFriends(self, gen_reply, is_good):
        data = self.api.friends.getRequests(extended=1)
        to_rep = []
        self.loadUsers(data['items'], lambda x:x['user_id'], True)
        for i in data['items']:
            if self.users[i['user_id']].get('blacklisted'):
                self.api.friends.delete.delayed(user_id=i['user_id'])
                continue
            res = is_good(i['user_id'], True)
            if res is None:
                self.api.friends.add.delayed(user_id=i['user_id'])
                text_msg = 'Adding ' + self.printableSender(i, False)
                html_msg = 'Adding ' + self.printableSender(i, True)
                log.info((text_msg, html_msg))
                if 'message' in i:
                    ans = gen_reply(i)
                    to_rep.append((i, ans))
            else:
                self.api.friends.delete.delayed(user_id=i['user_id'])
                text_msg = 'Not adding {} ({})'.format(self.printableSender(i, False), res)
                html_msg = 'Not adding {} ({})'.format(self.printableSender(i, True), res)
                log.info((text_msg, html_msg))
        for i in to_rep:
            self.replyMessage(i[0], i[1][0], i[1][1])
        self.api.sync()

    def unfollow(self, banned):
        result = []
        requests = self.api.friends.getRequests(out=1)['items'] + self.api.friends.getRequests(suggested=1)['items']
        for i in requests:
            if i not in banned:
                self.api.friends.delete.delayed(user_id=i)
                result.append(i)
        self.api.sync()
        return result

    def deleteFriend(self, uid):
        if type(uid) == int:
            self.api.friends.delete(user_id=uid)
        else:
            for i in uid:
                self.api.friends.delete.delayed(user_id=i)
            self.api.sync()

    def setOnline(self):
        self.api.account.setOnline()

    def getUserId(self, domain):
        multiple = type(domain) != str
        if not multiple:
            domain = [domain]
        domain = list(map(str.lower, domain))
        req = []
        for i in domain:
            i = str(i).rstrip().rstrip('}').rstrip()  # if id is in a forwarded message
            conf = re.search('sel=c(\\d+)', i) or re.search('^c(\\d+)$', i) or re.search('chat=(\\d+)', i) or re.search('peer=2(\\d{9})', i)
            if conf is not None:
                req.append(int(conf.group(1)) + CONF_START)
            else:
                if '=' in i:
                    i = i.split('=')[-1]
                if '/' in i:
                    i = i.split('/')[-1]
                req.append(int(i) if i.isdigit() else i)

        data = self.api.users.get(user_ids=','.join(i for i in req if type(i) == str), fields='domain')
        if data is None:
            return [] if multiple else None
        if len(req) == 1 and len(data) == 1:
            req = [data[0]['id']]  # kostil :(
        else:
            for i in range(len(req)):
                if type(req[i]) == str:
                    if data[0]['domain'] == req[i]:
                        req[i] = data[0]['id']
                        data = data[1:]
                    else:
                        req[i] = None
            req = [i for i in req if i is not None]
        try:
            if multiple:
                return req
            else:
                return req[0]
        except TypeError:
            return None

    def deleteComment(self, rep):
        if rep['type'].endswith('photo'):
            self.api.photos.deleteComment(owner_id=self.self_id, comment_id=rep['feedback']['id'])
        elif rep['type'].endswith('video'):
            self.api.video.deleteComment(owner_id=self.self_id, comment_id=rep['feedback']['id'])
        else:
            self.api.wall.deleteComment(owner_id=self.self_id, comment_id=rep['feedback']['id'])

    def filterComments(self, test, name_func):
        data = self.api.notifications.get(start_time=self.last_viewed_comment+1)['items']
        to_del = set()
        to_bl = set()
        self.loadUsers(data, lambda x:x['feedback']['from_id'], True)
        for rep in data:
            if rep['date'] != 'i':
                self.last_viewed_comment = max(self.last_viewed_comment, int(rep['date']))

            def _check(s):
                if 'photo' in s:
                    return s['photo']['owner_id'] == self.self_id
                if 'video' in s:
                    return s['video']['owner_id'] == self.self_id
                if 'post' in s:
                    return s['post']['to_id'] == self.self_id

            if rep['type'].startswith('comment_') or rep['type'].startswith('reply_comment') and _check(rep['parent']):
                txt = html.escape(rep['feedback']['text'])
                if self.users[rep['feedback']['from_id']]['blacklisted']:
                    text_msg = 'Comment {} (by {}) - blacklisted'.format(txt, name_func(rep['feedback']['from_id'], False))
                    html_msg = 'Comment {} (by {}) - blacklisted'.format(txt, name_func(rep['feedback']['from_id'], True))
                    log.write('comments', str(rep['feedback']['from_id']) + ' (blacklisted): ' + txt)
                    self.deleteComment(rep)
                    to_bl.add(rep['feedback']['from_id'])
                elif test(txt):
                    text_msg = 'Comment {} (by {}) - bad'.format(txt, name_func(rep['feedback']['from_id'], False))
                    html_msg = 'Comment {} (by {}) - bad'.format(txt, name_func(rep['feedback']['from_id'], True))
                    log.write('comments', str(rep['feedback']['from_id']) + ': ' + txt)
                    self.deleteComment(rep)
                    to_del.add(rep['feedback']['from_id'])
                elif 'attachments' in rep['feedback'] and  any(i.get('type') in ['video', 'link', 'doc', 'sticker'] for i in rep['feedback']['attachments']):
                    text_msg = 'Comment {} (by {}) - attachment'.format(txt, name_func(rep['feedback']['from_id'], False))
                    html_msg = 'Comment {} (by {}) - attachment'.format(txt, name_func(rep['feedback']['from_id'], True))
                    log.write('comments', str(rep['feedback']['from_id']) + ' (attachment)')
                    self.deleteComment(rep)
                else:
                    text_msg = 'Comment {} (by {}) - good'.format(txt, name_func(rep['feedback']['from_id'], False))
                    html_msg = 'Comment {} (by {}) - good'.format(txt, name_func(rep['feedback']['from_id'], True))
                log.info((text_msg, html_msg))
        for i in to_bl:
            self.blacklist(i)
        return to_del

    def likeAva(self, uid):
        del self.users[uid]
        if 'crop_photo' not in self.users[uid]:
            log.write('likeava', str(uid) + ' missing')
            return
        photo = self.users[uid]['crop_photo']['photo']
        log.write('likeava', str(uid))
        self.api.likes.add(type='photo', owner_id=photo['owner_id'], item_id=photo['id'])

    def setRelation(self, uid):
        try:
            self.api.account.saveProfileInfo(relation_partner_id=uid)
            self.bf = self.users[uid]
            log.write('relation', uid)
        except Exception:
            log.write('relation', str(uid) + ' failed')

    def waitAllThreads(self):
        for t in self.tm.all():
            t.join(60)

    # {name} - first_name last_name
    # {id} - id
    def printableName(self, pid, user_fmt = '<a href="https://vk.com/id{id}" target="_blank">{name}</a>', conf_fmt = 'Conf {id}'):
        if pid > CONF_START:
            return conf_fmt.format(id=(pid - CONF_START))
        else:
            return user_fmt.format(id=(pid), name=self.users[pid]['first_name'] + ' ' + self.users[pid]['last_name'])

    def printableSender(self, message, need_html):
        if message.get('chat_id', 0) > 0:
            if need_html:
                return self.printableName(message['user_id'], user_fmt='Conf %c, <a href="https://vk.com/id{id}" target="_blank">{name}</a>').replace('%c', str(message['chat_id']))
            else:
                return self.printableName(message['user_id'], user_fmt='Conf %c, {name}').replace('%c', str(message['chat_id']))
        else:
            if need_html:
                return self.printableName(message['user_id'], user_fmt='<a href="https://vk.com/id{id}" target="_blank">{name}</a>')
            else:
                return self.printableName(message['user_id'], user_fmt='{name}')

    def blacklist(self, uid):
        self.api.account.banUser(user_id=uid)

    def dialogCount(self):
        return  self.api.messages.getDialogs(count=0)['count']
