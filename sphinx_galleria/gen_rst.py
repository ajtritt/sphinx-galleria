# -*- coding: utf-8 -*-
# Author: Óscar Nájera
# License: 3-clause BSD
"""
RST file generator
==================

Generate the rst files for the examples by iterating over the python
example files.

Files that generate images should start with 'plot'

"""
# Don't use unicode_literals here (be explicit with u"..." instead) otherwise
# tricky errors come up with exec(code_blocks, ...) calls
from __future__ import division, print_function, absolute_import
from time import time
import ast
import codecs
import hashlib
import os
import re
import shutil
import subprocess
import sys
import traceback
import codeop
from distutils.version import LooseVersion

from .utils import replace_py_ipynb


# Try Python 2 first, otherwise load from Python 3
try:
    # textwrap indent only exists in python 3
    from textwrap import indent
except ImportError:
    def indent(text, prefix, predicate=None):
        """Adds 'prefix' to the beginning of selected lines in 'text'.

        If 'predicate' is provided, 'prefix' will only be added to the lines
        where 'predicate(line)' is True. If 'predicate' is not provided,
        it will default to adding 'prefix' to all non-empty lines that do not
        consist solely of whitespace characters.
        """
        if predicate is None:
            def predicate(line):
                return line.strip()

        def prefixed_lines():
            for line in text.splitlines(True):
                yield (prefix + line if predicate(line) else line)
        return ''.join(prefixed_lines())

from io import StringIO

# make sure that the Agg backend is set before importing any
# matplotlib
import matplotlib
matplotlib.use('agg')
matplotlib_backend = matplotlib.get_backend()

if matplotlib_backend != 'agg':
    mpl_backend_msg = (
        "Sphinx-Gallery relies on the matplotlib 'agg' backend to "
        "render figures and write them to files. You are "
        "currently using the {} backend. Sphinx-Gallery will "
        "terminate the build now, because changing backends is "
        "not well supported by matplotlib. We advise you to move "
        "sphinx_galleria imports before any matplotlib-dependent "
        "import. Moving sphinx_galleria imports at the top of "
        "your conf.py file should fix this issue")

    raise ValueError(mpl_backend_msg.format(matplotlib_backend))

import matplotlib.pyplot as plt
import sphinx

from . import glr_path_static
from . import sphinx_compatibility
from .backreferences import write_backreferences, _thumbnail_div
from .downloads import CODE_DOWNLOAD
from .py_source_parser import split_code_and_text_blocks

from .notebook import jupyter_notebook, save_notebook
from .binder import check_binder_conf, copy_binder_reqs, gen_binder_rst

try:
    basestring
except NameError:
    basestring = str
    unicode = str

logger = sphinx_compatibility.getLogger('sphinx-gallery')


###############################################################################


class LoggingTee(object):
    """A tee object to redirect streams to the logger"""

    def __init__(self, output_file, logger, src_filename):
        self.output_file = output_file
        self.logger = logger
        self.src_filename = src_filename
        self.first_write = True
        self.logger_buffer = ''

    def write(self, data):
        self.output_file.write(data)

        if self.first_write:
            self.logger.verbose('Output from %s', self.src_filename,
                                color='brown')
            self.first_write = False

        data = self.logger_buffer + data
        lines = data.splitlines()
        if data and data[-1] not in '\r\n':
            # Wait to write last line if it's incomplete. It will write next
            # time or when the LoggingTee is flushed.
            self.logger_buffer = lines[-1]
            lines = lines[:-1]
        else:
            self.logger_buffer = ''

        for line in lines:
            self.logger.verbose('%s', line)

    def flush(self):
        self.output_file.flush()
        if self.logger_buffer:
            self.logger.verbose('%s', self.logger_buffer)
            self.logger_buffer = ''

    # When called from a local terminal seaborn needs it in Python3
    def isatty(self):
        return self.output_file.isatty()


class MixedEncodingStringIO(StringIO):
    """Helper when both ASCII and unicode strings will be written"""

    def write(self, data):
        if not isinstance(data, unicode):
            data = data.decode('utf-8')
        StringIO.write(self, data)


###############################################################################
# The following strings are used when we have several pictures: we use
# an html div tag that our CSS uses to turn the lists into horizontal
# lists.
HLIST_HEADER = """
.. rst-class:: sphx-glr-horizontal

"""

HLIST_IMAGE_TEMPLATE = """
    *

      .. image:: /%s
            :class: sphx-glr-multi-img
"""

SINGLE_IMAGE = """
.. image:: /%s
    :class: sphx-glr-single-img
"""


# This one could contain unicode
CODE_OUTPUT = u""".. rst-class:: sphx-glr-script-out

 Out::

{0}\n"""


SPHX_GLR_SIG = """\n
.. only:: html

 .. rst-class:: sphx-glr-signature

    `Gallery generated by Sphinx-Gallery <https://sphinx-gallery.readthedocs.io>`_\n"""


def codestr2rst(codestr, lang='python', lineno=None):
    """Return reStructuredText code block from code string"""
    if lineno is not None:
        if LooseVersion(sphinx.__version__) >= '1.3':
            # Sphinx only starts numbering from the first non-empty line.
            blank_lines = codestr.count('\n', 0, -len(codestr.lstrip()))
            lineno = '   :lineno-start: {0}\n'.format(lineno + blank_lines)
        else:
            lineno = '   :linenos:\n'
    else:
        lineno = ''
    code_directive = "\n.. code-block:: {0}\n{1}\n".format(lang, lineno)
    indented_block = indent(codestr, ' ' * 4)
    return code_directive + indented_block


def extract_intro_and_title(filename, docstring):
    """ Extract the first paragraph of module-level docstring. max:95 char"""

    # lstrip is just in case docstring has a '\n\n' at the beginning
    paragraphs = docstring.lstrip().split('\n\n')
    # remove comments and other syntax like `.. _link:`
    paragraphs = [p for p in paragraphs
                  if not p.startswith('.. ') and len(p) > 0]
    if len(paragraphs) == 0:
        raise ValueError(
            "Example docstring should have a header for the example title. "
            "Please check the example file:\n {}\n".format(filename))
    # Title is the first paragraph with any ReSTructuredText title chars
    # removed, i.e. lines that consist of (all the same) 7-bit non-ASCII chars.
    # This conditional is not perfect but should hopefully be good enough.
    title_paragraph = paragraphs[0]
    match = re.search(r'([\w ]+)', title_paragraph)

    if match is None:
        raise ValueError(
            'Could not find a title in first paragraph:\n{}'.format(
                         title_paragraph))
    title = match.group(1).strip()
    # Use the title if no other paragraphs are provided
    intro_paragraph = title if len(paragraphs) < 2 else paragraphs[1]
    # Concatenate all lines of the first paragraph and truncate at 95 chars
    intro = re.sub('\n', ' ', intro_paragraph)
    if len(intro) > 95:
        intro = intro[:95] + '...'

    return intro, title


def get_md5sum(src_file):
    """Returns md5sum of file"""

    with open(src_file, 'rb') as src_data:
        src_content = src_data.read()

        src_md5 = hashlib.md5(src_content).hexdigest()
    return src_md5


def md5sum_is_current(src_file):
    """Checks whether src_file has the same md5 hash as the one on disk"""

    src_md5 = get_md5sum(src_file)

    src_md5_file = src_file + '.md5'
    if os.path.exists(src_md5_file):
        with open(src_md5_file, 'r') as file_checksum:
            ref_md5 = file_checksum.read()

        return src_md5 == ref_md5

    return False


def save_figures(image_path, fig_count, gallery_conf):
    """Save all open matplotlib figures of the example code-block

    Parameters
    ----------
    image_path : str
        Path where plots are saved (format string which accepts figure number)
    fig_count : int
        Previous figure number count. Figure number add from this number
    gallery_conf : dict
        Contains the configuration of Sphinx-Gallery

    Returns
    -------
    images_rst : str
        rst code to embed the images in the document
    fig_num : int
        number of figures saved
    """
    figure_list = []

    for fig_num in plt.get_fignums():
        # Set the fig_num figure as the current figure as we can't
        # save a figure that's not the current figure.
        fig = plt.figure(fig_num)
        kwargs = {}
        to_rgba = matplotlib.colors.colorConverter.to_rgba
        for attr in ['facecolor', 'edgecolor']:
            fig_attr = getattr(fig, 'get_' + attr)()
            default_attr = matplotlib.rcParams['figure.' + attr]
            if to_rgba(fig_attr) != to_rgba(default_attr):
                kwargs[attr] = fig_attr

        current_fig = image_path.format(fig_count + fig_num)
        fig.savefig(current_fig, **kwargs)
        figure_list.append(current_fig)

    if gallery_conf.get('find_mayavi_figures', False):
        from mayavi import mlab
        e = mlab.get_engine()
        last_matplotlib_fig_num = fig_count + len(figure_list)
        total_fig_num = last_matplotlib_fig_num + len(e.scenes)
        mayavi_fig_nums = range(last_matplotlib_fig_num + 1, total_fig_num + 1)

        for scene, mayavi_fig_num in zip(e.scenes, mayavi_fig_nums):
            current_fig = image_path.format(mayavi_fig_num)
            mlab.savefig(current_fig, figure=scene)
            # make sure the image is not too large
            scale_image(current_fig, current_fig, 850, 999)
            figure_list.append(current_fig)
        mlab.close(all=True)

    return figure_rst(figure_list, gallery_conf['src_dir'])


def figure_rst(figure_list, sources_dir):
    """Given a list of paths to figures generate the corresponding rst

    Depending on whether we have one or more figures, we use a
    single rst call to 'image' or a horizontal list.

    Parameters
    ----------
    figure_list : list of str
        Strings are the figures' absolute paths
    sources_dir : str
        absolute path of Sphinx documentation sources

    Returns
    -------
    images_rst : str
        rst code to embed the images in the document
    fig_num : int
        number of figures saved
    """

    figure_paths = [os.path.relpath(figure_path, sources_dir)
                    .replace(os.sep, '/').lstrip('/')
                    for figure_path in figure_list]
    images_rst = ""
    if len(figure_paths) == 1:
        figure_name = figure_paths[0]
        images_rst = SINGLE_IMAGE % figure_name
    elif len(figure_paths) > 1:
        images_rst = HLIST_HEADER
        for figure_name in figure_paths:
            images_rst += HLIST_IMAGE_TEMPLATE % figure_name

    return images_rst, len(figure_list)


def scale_image(in_fname, out_fname, max_width, max_height):
    """Scales an image with the same aspect ratio centered in an
       image with a given max_width and max_height
       if in_fname == out_fname the image can only be scaled down
    """
    # local import to avoid testing dependency on PIL:
    try:
        from PIL import Image
    except ImportError:
        import Image
    img = Image.open(in_fname)
    width_in, height_in = img.size
    scale_w = max_width / float(width_in)
    scale_h = max_height / float(height_in)

    if height_in * scale_w <= max_height:
        scale = scale_w
    else:
        scale = scale_h

    if scale >= 1.0 and in_fname == out_fname:
        return

    width_sc = int(round(scale * width_in))
    height_sc = int(round(scale * height_in))

    # resize the image using resize; if using .thumbnail and the image is
    # already smaller than max_width, max_height, then this won't scale up
    # at all (maybe could be an option someday...)
    img = img.resize((width_sc, height_sc), Image.BICUBIC)
    # img.thumbnail((width_sc, height_sc), Image.BICUBIC)
    # width_sc, height_sc = img.size  # necessary if using thumbnail

    # insert centered
    thumb = Image.new('RGB', (max_width, max_height), (255, 255, 255))
    pos_insert = ((max_width - width_sc) // 2, (max_height - height_sc) // 2)
    thumb.paste(img, pos_insert)

    thumb.save(out_fname)


def save_thumbnail(image_path_template, src_file, file_conf, gallery_conf):
    """Save the thumbnail image"""
    # read specification of the figure to display as thumbnail from main text
    thumbnail_number = file_conf.get('thumbnail_number', 1)
    if not isinstance(thumbnail_number, int):
        raise TypeError(
            'sphinx_galleria_thumbnail_number setting is not a number.')
    thumbnail_image_path = image_path_template.format(thumbnail_number)

    thumb_dir = os.path.join(os.path.dirname(thumbnail_image_path), 'thumb')
    if not os.path.exists(thumb_dir):
        os.makedirs(thumb_dir)

    base_image_name = os.path.splitext(os.path.basename(src_file))[0]
    thumb_file = os.path.join(thumb_dir,
                              'sphx_glr_%s_thumb.png' % base_image_name)

    if src_file in gallery_conf['failing_examples']:
        img = os.path.join(glr_path_static(), 'broken_example.png')
    elif os.path.exists(thumbnail_image_path):
        img = thumbnail_image_path
    elif not os.path.exists(thumb_file):
        # create something to replace the thumbnail
        img = os.path.join(glr_path_static(), 'no_image.png')
        img = gallery_conf.get("default_thumb_file", img)
    else:
        return
    scale_image(img, thumb_file, *gallery_conf["thumbnail_size"])


def generate_dir_rst(src_dir, target_dir, gallery_conf, seen_backrefs):
    """Generate the gallery reStructuredText for an example directory"""

    with codecs.open(os.path.join(src_dir, 'README.txt'), 'r',
                     encoding='utf-8') as fid:
        fhindex = fid.read()
    # Add empty lines to avoid bug in issue #165
    fhindex += "\n\n"

    if not os.path.exists(target_dir):
        os.makedirs(target_dir)
    # get filenames
    listdir = [fname for fname in os.listdir(src_dir)
               if fname.endswith('.py')]
    # limit which to look at based on regex (similar to filename_pattern)
    listdir = [fname for fname in listdir
               if re.search(gallery_conf['ignore_pattern'],
                            os.path.normpath(os.path.join(src_dir, fname)))
               is None]
    # sort them
    sorted_listdir = sorted(
        listdir, key=gallery_conf['within_subsection_order'](src_dir))
    entries_text = []
    computation_times = []
    build_target_dir = os.path.relpath(target_dir, gallery_conf['src_dir'])
    iterator = sphinx_compatibility.status_iterator(
        sorted_listdir,
        'generating gallery for %s... ' % build_target_dir,
        length=len(sorted_listdir))
    for fname in iterator:
        intro, time_elapsed = generate_file_rst(
            fname,
            target_dir,
            src_dir,
            gallery_conf)
        computation_times.append((time_elapsed, fname))
        this_entry = _thumbnail_div(build_target_dir, fname, intro) + """

.. toctree::
   :hidden:

   /%s\n""" % os.path.join(build_target_dir, fname[:-3]).replace(os.sep, '/')
        entries_text.append(this_entry)

        if gallery_conf['backreferences_dir']:
            write_backreferences(seen_backrefs, gallery_conf,
                                 target_dir, fname, intro)

    for entry_text in entries_text:
        fhindex += entry_text

    # clear at the end of the section
    fhindex += """.. raw:: html\n
    <div style='clear:both'></div>\n\n"""

    return fhindex, computation_times


def handle_exception(exc_info, src_file, block_vars, gallery_conf):
    etype, exc, tb = exc_info
    stack = traceback.extract_tb(tb)
    # Remove our code from traceback:
    if isinstance(exc, SyntaxError):
        # Remove one extra level through ast.parse.
        stack = stack[2:]
    else:
        stack = stack[1:]
    formatted_exception = 'Traceback (most recent call last):\n' + ''.join(
        traceback.format_list(stack) +
        traceback.format_exception_only(etype, exc))

    logger.warning('%s failed to execute correctly: %s', src_file,
                   formatted_exception)

    except_rst = codestr2rst(formatted_exception, lang='pytb')

    # Breaks build on first example error
    if gallery_conf['abort_on_example_error']:
        raise
    # Stores failing file
    gallery_conf['failing_examples'][src_file] = formatted_exception
    block_vars['execute_script'] = False

    return except_rst


def execute_code_block(compiler, src_file, code_block, lineno, example_globals,
                       block_vars, gallery_conf):
    """Executes the code block of the example file"""
    time_elapsed = 0

    # If example is not suitable to run, skip executing its blocks
    if not block_vars['execute_script']:
        return '', time_elapsed

    plt.close('all')
    cwd = os.getcwd()
    # Redirect output to stdout and
    orig_stdout = sys.stdout
    src_file = block_vars['src_file']

    # First cd in the original example dir, so that any file
    # created by the example get created in this directory

    my_stdout = MixedEncodingStringIO()
    os.chdir(os.path.dirname(src_file))
    sys.stdout = LoggingTee(my_stdout, logger, src_file)

    try:
        dont_inherit = 1
        code_ast = compile(code_block, src_file, 'exec',
                           ast.PyCF_ONLY_AST | compiler.flags, dont_inherit)
        ast.increment_lineno(code_ast, lineno - 1)
        t_start = time()
        # don't use unicode_literals at the top of this file or you get
        # nasty errors here on Py2.7
        exec(compiler(code_ast, src_file, 'exec'), example_globals)
        time_elapsed = time() - t_start
    except Exception:
        sys.stdout.flush()
        sys.stdout = orig_stdout
        except_rst = handle_exception(sys.exc_info(), src_file, block_vars,
                                      gallery_conf)
        # python2.7: Code was read in bytes needs decoding to utf-8
        # unless future unicode_literals is imported in source which
        # make ast output unicode strings
        if hasattr(except_rst, 'decode') and not isinstance(except_rst, unicode):
            except_rst = except_rst.decode('utf-8')

        code_output = u"\n{0}\n\n\n\n".format(except_rst)

    else:
        sys.stdout.flush()
        sys.stdout = orig_stdout
        os.chdir(cwd)

        my_stdout = my_stdout.getvalue().strip().expandtabs()
        if my_stdout:
            stdout = CODE_OUTPUT.format(indent(my_stdout, u' ' * 4))
        else:
            stdout = ''
        images_rst, fig_num = save_figures(block_vars['image_path'],
                                           block_vars['fig_count'],
                                           gallery_conf)

        block_vars['fig_count'] += fig_num
        code_output = u"\n{0}\n\n{1}\n\n".format(images_rst, stdout)

    finally:
        os.chdir(cwd)
        sys.stdout = orig_stdout

    return code_output, time_elapsed


def clean_modules():
    """Remove "unload" seaborn from the name space

    After a script is executed it can load a variety of setting that one
    does not want to influence in other examples in the gallery."""

    # Horrible code to 'unload' seaborn, so that it resets
    # its default when is load
    # Python does not support unloading of modules
    # https://bugs.python.org/issue9072
    for module in list(sys.modules.keys()):
        if 'seaborn' in module:
            del sys.modules[module]

    # Reset Matplotlib to default
    plt.rcdefaults()


def generate_file_rst(fname, target_dir, src_dir, gallery_conf):
    """Generate the rst file for a given example.

    Returns
    -------
    intro: str
        The introduction of the example
    time_elapsed : float
        seconds required to run the script
    """
    binder_conf = check_binder_conf(gallery_conf.get('binder'))
    src_file = os.path.normpath(os.path.join(src_dir, fname))
    example_file = os.path.join(target_dir, fname)
    shutil.copyfile(src_file, example_file)
    file_conf, script_blocks = split_code_and_text_blocks(src_file)
    intro, title = extract_intro_and_title(fname, script_blocks[0][1])

    if md5sum_is_current(example_file):
        return intro, 0

    image_dir = os.path.join(target_dir, 'images')
    if not os.path.exists(image_dir):
        os.makedirs(image_dir)

    base_image_name = os.path.splitext(fname)[0]
    image_fname = 'sphx_glr_' + base_image_name + '_{0:03}.png'
    image_path_template = os.path.join(image_dir, image_fname)

    ref_fname = os.path.relpath(example_file, gallery_conf['src_dir'])
    ref_fname = ref_fname.replace(os.path.sep, '_')
    example_rst = """\n\n.. _sphx_glr_{0}:\n\n""".format(ref_fname)

    filename_pattern = gallery_conf.get('filename_pattern')
    execute_script = re.search(filename_pattern, src_file) and gallery_conf[
        'plot_gallery']
    example_globals = {
        # A lot of examples contains 'print(__doc__)' for example in
        # scikit-learn so that running the example prints some useful
        # information. Because the docstring has been separated from
        # the code blocks in sphinx-gallery, __doc__ is actually
        # __builtin__.__doc__ in the execution context and we do not
        # want to print it
        '__doc__': '',
        # Examples may contain if __name__ == '__main__' guards
        # for in example scikit-learn if the example uses multiprocessing
        '__name__': '__main__',
        # Don't ever support __file__: Issues #166 #212
    }
    compiler = codeop.Compile()

    # A simple example has two blocks: one for the
    # example introduction/explanation and one for the code
    is_example_notebook_like = len(script_blocks) > 2
    time_elapsed = 0
    block_vars = {'execute_script': execute_script, 'fig_count': 0,
                  'image_path': image_path_template, 'src_file': src_file}

    argv_orig = sys.argv[:]
    if block_vars['execute_script']:
        # We want to run the example without arguments. See
        # https://github.com/sphinx-gallery/sphinx-gallery/pull/252
        # for more details.
        sys.argv[0] = src_file
        sys.argv[1:] = []

    for blabel, bcontent, lineno in script_blocks:
        if blabel == 'code':
            code_output, rtime = execute_code_block(compiler, src_file,
                                                    bcontent, lineno,
                                                    example_globals,
                                                    block_vars, gallery_conf)

            time_elapsed += rtime

            if not file_conf.get('line_numbers',
                                 gallery_conf.get('line_numbers', False)):
                lineno = None

            if is_example_notebook_like:
                example_rst += codestr2rst(bcontent, lineno=lineno) + '\n'
                example_rst += code_output
            else:
                example_rst += code_output
                if 'sphx-glr-script-out' in code_output:
                    # Add some vertical space after output
                    example_rst += "\n\n|\n\n"
                example_rst += codestr2rst(bcontent, lineno=lineno) + '\n'

        else:
            example_rst += bcontent + '\n\n'

    sys.argv = argv_orig
    clean_modules()

    # Writes md5 checksum if example has build correctly
    # not failed and was initially meant to run(no-plot shall not cache md5sum)
    if block_vars['execute_script']:
        with open(example_file + '.md5', 'w') as file_checksum:
            file_checksum.write(get_md5sum(example_file))

    save_thumbnail(image_path_template, src_file, file_conf, gallery_conf)

    time_m, time_s = divmod(time_elapsed, 60)
    example_nb = jupyter_notebook(script_blocks)
    save_notebook(example_nb, replace_py_ipynb(example_file))
    with codecs.open(os.path.join(target_dir, base_image_name + '.rst'),
                     mode='w', encoding='utf-8') as f:
        if time_elapsed >= gallery_conf["min_reported_time"]:
            example_rst += ("**Total running time of the script:**"
                            " ({0: .0f} minutes {1: .3f} seconds)\n\n".format(
                                time_m, time_s))
        # Generate a binder URL if specified
        binder_badge_rst = ''
        if len(binder_conf) > 0:
            binder_badge_rst += gen_binder_rst(fname, binder_conf)

        example_rst += CODE_DOWNLOAD.format(fname,
                                            replace_py_ipynb(fname),
                                            binder_badge_rst)
        example_rst += SPHX_GLR_SIG
        f.write(example_rst)

    if block_vars['execute_script']:
        logger.debug("%s ran in : %.2g seconds", src_file, time_elapsed)

    return intro, time_elapsed
