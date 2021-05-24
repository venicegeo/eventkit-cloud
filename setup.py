import os
from setuptools import setup, find_packages


def read(*rnames):
    return open(os.path.join(os.path.dirname(__file__), *rnames)).read()


setup(
    name="eventkit",
    version="1.10.0",
    author="Joseph Svrcek",
    author_email="joseph.svrcek@rgi-corp.com",
    description="Eventkit Cloud",
    long_description=(read('readme.md')),
    # Full list of classifiers can be found at:
    # http://pypi.python.org/pypi?%3Aaction=list_classifiers
    classifiers=[
        'Development Status :: 4 - Beta',
        'Framework :: Django',
        'Operating System :: POSIX :: Linux',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: JavaScript'
    ],
    license="BSD",
    keywords="eventkit osm-export-tool django mapproxy",
    packages=find_packages(exclude=["*.tests", "*.tests.*", "tests.*", "tests"]),
    include_package_data=True,
    scripts=['manage.py']
)
