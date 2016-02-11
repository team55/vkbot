import time
import config
import traceback


def info(s, color=''):
    if color == 'red':
        print('\033[38;5;9m' + s + '\033[0m')
    elif color == 'green':
        print('\033[38;5;10m' + s + '\033[0m')
    elif color == 'yellow':
        print('\033[38;5;11m' + s + '\033[0m')
    else:
        print(s)

def warning(s):
    print('[WARNING]', s)

def error(s, need_exc_info=False):
    print('[ERROR]', s)
    write('error', s)
    if need_exc_info:
        with open('logs/error.log', 'a') as f:
            traceback.print_exc(file=f)
            print(file=f)

def fatal(s):
    print('[FATAL]', s)
    exit(0)


datetime_format = config.get('log.datetime_format')

def write(log, s):
    curtime = time.strftime(datetime_format, time.localtime())
    with open('logs/{}.log'.format(log), 'a') as f:
        f.write('[{}] {}\n'.format(curtime, s))
